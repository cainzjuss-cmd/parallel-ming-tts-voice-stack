"""Shared-weights launcher: load Ming ONCE on the GPU, then spawn ComfyUI workers
that map the same physical VRAM through CUDA IPC handles instead of loading copies.

Verified mechanism first (cache/scratch/ipc2.py): a child process read a 1.3 GiB CUDA
tensor allocated by another process with identical contents and its own allocator
reported 0.000 GB; the card grew by ~300 MiB (that child's CUDA context), not by a
second copy. The node makes this bindable: loader.py:192 uses
load_state_dict(..., assign=True), which takes the tensors it is handed as-is.

The children are spawned by THIS process on purpose: the handle exchange then runs
through a real multiprocessing queue, the same reduction path ipc2.py proved. Do not
start the workers separately - they share the parent's lifetime, because an IPC
mapping dies if the holder does.

    MING_WORKER_PORTS=8188,8189,8190 python tools/ming_weightd.py
"""
import importlib, importlib.util, os, re, subprocess, sys, time

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:            # pythonNN._pth on the embedded interpreter excludes it;
    sys.path.insert(0, TOOLS)        # spawn also re-imports this module in every child
import stack_paths as sp

ROOT = os.path.join(sp.COMFY_ROOT, "ComfyUI")
NODE = os.path.join(ROOT, "custom_nodes", "ComfyUI-Ming-3-tts")
SPOOL = os.path.join(TOOLS, "dow_spool.py")
PORTS = [int(p) for p in os.environ.get("MING_WORKER_PORTS", "").split(",") if p.strip()]   # empty = follow pool.json["workers"]

sys.argv = [sys.argv[0]]                      # comfy.cli_args parses argv at import
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS)
import tts_pool                               # needs the insert above; single source of truth


def _holder_env():
    """Import ComfyUI machinery. MUST stay inside a function: Windows spawn re-imports
    this module in every child at bootstrap, and utils.extra_config / the ming3 package
    pull in comfy.cli_args, which would parse the child's argv before main() sets it -
    that silently gave every worker the default port 8188."""
    import importlib
    spec = importlib.util.spec_from_file_location("ming3", os.path.join(NODE, "__init__.py"),
                                                  submodule_search_locations=[NODE])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["ming3"] = pkg
    spec.loader.exec_module(pkg)
    import utils.extra_config
    cfg = os.path.join(ROOT, "extra_model_paths.yaml")
    if os.path.isfile(cfg):
        utils.extra_config.load_extra_path_config(cfg)
    loader = importlib.import_module("ming3.loader")
    MING_MODEL = eval(re.search(r"^MING_MODEL\s*=\s*(.+)$", open(SPOOL, encoding="utf8").read(), re.M).group(1))
    return loader, MING_MODEL


def gpu_used():
    """MiB used on the card, via the shim's nvidia-smi read (sp.gpu_mem). nvidia-smi answers
    whole MiB, so the GB round-trip inside gpu_mem() stays exact here; failure is -1 as before."""
    g = sp.gpu_mem()
    return int(round(g[1] * 1024.0)) if g else -1


def warm_reference():
    """Copy an installed voice's reference clip into ComfyUI's input directory and return
    (input-relative filename, transcript, language), or None.

    This exists because warming a worker with the ip task does NOT warm the path replies
    actually take: only the clone task touches the speaker encoder (assets/campplus.onnx)
    and the clone decode, and that first-run work measured 22.1 s on the first reply after
    boot against 5.0 s on the second and 1.8 s on the third."""
    try:
        import json
        import shutil
        voices = sp.VOICES     # the voices root from the shim; the copy below lands in
                               # ROOT/input (COMFY-anchored), both now env/config-resolved
        for vid in sorted(os.listdir(voices)):
            d = os.path.join(voices, vid)
            wav, txt = os.path.join(d, "ref.wav"), os.path.join(d, "ref_text.txt")
            if not (os.path.isfile(wav) and os.path.isfile(txt)):
                continue
            dst = os.path.join(ROOT, "input", "dow_warm_ref.wav")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(wav):
                shutil.copyfile(wav, dst)
            lang = "English"
            try:
                with open(os.path.join(d, "voice.json"), encoding="utf8") as fh:
                    lang = json.load(fh).get("language", "English")
            except Exception:
                pass
            with open(txt, encoding="utf8") as fh:
                return "dow_warm_ref.wav", fh.read().strip()[:400], lang
    except Exception as exc:
        print("warm: no reference clip available (%r), falling back to non-clone tasks"
              % (exc,), flush=True)
    return None


def warm(port, model_name, precision):
    """Send one throwaway generation to a worker, while no other worker is loading.

    A worker's FIRST generation makes that process pull its OWN copy of the weights onto the
    GPU before unpack() rebinds it to the shared set. If the spooler's first prompts hit three
    workers at once, that is three private copies racing the card: in fp32, 3 x 4.8 GB on top
    of the holder's 4.8 GB and the desktop's ~13 GB, against a 24.5 GB card. The workers then
    die with 0xC0000409 - a native fastfail, no Python traceback - and the holder correctly
    takes the whole pool down. Warming sequentially caps that transient at one copy, and it
    also removes the 10-30 s first-reply penalty from the user's side.
    """
    import json, urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:%d" % port
    for i in range(180):                                   # worker boot: imports, ComfyUI start
        try:
            opener.open(base + "/system_stats", timeout=4).read()
            break
        except Exception:
            time.sleep(1)
    else:
        print("warm %d: port never came up, skipping" % port, flush=True)
        return
    before = gpu_used()
    # DynamicCombo V3 on this build (object_info/MingGenerate -> "COMFY_DYNAMICCOMBO_V3"):
    # the task is a plain string and its conditioning inputs travel as flat "task.<name>"
    # keys, exactly as tools/dow_spool.py sends the clone path. clone first, because that is
    # the task production replies use and the only one that warms the speaker encoder; ip and
    # voice_design remain as fallbacks for when no reference clip can be staged.
    graphs = []
    ref = warm_reference()
    if ref:
        rel, rtext, lang = ref
        graphs.append(("clone", {"task": "clone", "task.reference_audio": ["1", 0],
                                 "task.reference_text": rtext, "task.language": lang}, True))
    graphs.append(("ip", {"task": "ip", "task.voice": "US_James"}, False))
    graphs.append(("voice_design", {"task": "voice_design", "task.style": "neutral male voice"}, False))
    for label, extra, want_audio in graphs:
        graph = {
            "2": {"class_type": "MingLoadModel",
                  "inputs": {"model": model_name, "precision": precision,
                             "device": "auto", "download_if_missing": False}},
            "3": {"class_type": "MingGenerate",
                  "inputs": {"model": ["2", 0], "text": "Yes brother.", "seed": 1, "cfg": 2.0,
                             "flow_steps": 2, "sigma": 0.25, "temperature": 0.0,
                             "max_steps": 16, "trim": True, **extra}},
            "4": {"class_type": "SaveAudio", "inputs": {"audio": ["3", 0], "filename_prefix": "warm"}},
        }
        if want_audio:
            graph["1"] = {"class_type": "LoadAudio", "inputs": {"audio": rel}}
        try:
            req = urllib.request.Request(base + "/prompt", data=json.dumps({"prompt": graph}).encode(),
                                         headers={"Content-Type": "application/json"})
            pid = json.loads(opener.open(req, timeout=20).read())["prompt_id"]
            for _ in range(240):                            # first load is the slow part
                h = json.loads(opener.open(base + "/history/" + pid, timeout=10).read())
                if pid in h:
                    st = (h[pid].get("status") or {})
                    print("warm %d: %s %s (gpu %d -> %d MiB)"
                          % (port, label, "ok" if st.get("completed") else st.get("status_str", "?"),
                             before, gpu_used()), flush=True)
                    if st.get("completed"):
                        return True
                    break
                time.sleep(1)
        except Exception as exc:
            print("warm %d: %s failed %r" % (port, label, exc), flush=True)
    print("warm %d: could not warm with either task; gpu back to %d MiB" % (port, gpu_used()), flush=True)
    return False


def _mem():
    """(private commit GB, working set GB) for this process. Commit is the number that matters
    for the commit-exhaustion failure class and comes from sp.priv_gb (psapi PagefileUsage on
    Windows - the same read the old inline PMC did - Private_Dirty on Linux). Only the
    working-set half is platform code, and it is holder-log diagnostics, not load-bearing."""
    commit = sp.priv_gb(os.getpid())
    if commit < 0:
        return (0.0, 0.0)
    if sp.OS == "windows":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(pmc), pmc.cb):
            return (commit, 0.0)     # psapi exports GetProcessMemoryInfo directly here; the
        return (commit, pmc.WorkingSetSize / 1e9)   # K32 form is a header macro
    try:
        with open("/proc/self/status", encoding="utf8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return (commit, int(line.split()[1]) * 1024.0 / 1e9)
    except Exception:
        pass
    return (commit, 0.0)


def empty_working_set():
    if sp.OS != "windows":
        return False          # EmptyWorkingSet has no POSIX equivalent; the kernel already
                              # evicts file-backed pages first. The caller only logs the delta.
    import ctypes
    try:
        ctypes.windll.psapi.EmptyWorkingSet(
            ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()))
        return True
    except Exception:
        return False


def main():
    import gc
    import torch
    import ming_worker
    loader, MING_MODEL = _holder_env()
    print("loading %s once (pid %d)" % (MING_MODEL, os.getpid()), flush=True)
    base = gpu_used()
    cfg = tts_pool.settings()
    precision, ports = cfg["precision"], (PORTS or tts_pool.ports())
    bundle = loader.load_model(MING_MODEL, precision, "auto")
    model = bundle.patcher.model
    dev = bundle.patcher.load_device
    print("loaded; load_device=%s, gpu %d MiB" % (dev, gpu_used()), flush=True)

    m_load = _mem()
    payload = ming_worker.pack(model, dev)
    shared = payload                           # plain tensors + flattened quant subclasses
    torch.cuda.synchronize()
    m_pack = _mem()
    # bind the holder's own model to the GPU copy as well, which releases its CPU copies
    bound, bad = ming_worker.unpack(payload, model)
    m_bind = _mem()
    # The holder had to read the checkpoint and dequantize on the host before any of it
    # could be published, and several GB of that stays charged to the process afterwards:
    # the tensors are dereferenced but the CRT keeps the arenas. Staged reclaim, each step
    # logged, so it is visible which call moves private commit and which only moves
    # physical residency, rather than assuming EmptyWorkingSet is enough (it trims the
    # working set only; commit is what exhausted the machine for the fp32 workers).
    if os.environ.get("DOW_HOLDER_TRIM", "1") != "0":
        gc.collect()
        a_gc = _mem()
        torch.cuda.empty_cache()
        a_cache = _mem()
        empty_working_set()
        a_ws = _mem()
        print("holder memory GB (commit/workingset): after load=%.1f/%.1f  after pack=%.1f/%.1f "
              "after bind=%.1f/%.1f  | gc=%.1f/%.1f  cuda.empty_cache=%.1f/%.1f  "
              "EmptyWorkingSet=%.1f/%.1f"
              % (m_load[0], m_load[1], m_pack[0], m_pack[1], m_bind[0], m_bind[1],
                 a_gc[0], a_gc[1], a_cache[0], a_cache[1], a_ws[0], a_ws[1]), flush=True)
    print("bound holder model: %d tensors + %d quant subclass(es) (%d problem(s)), gpu %d MiB (baseline %d)"
          % (len(payload["t"]), len(payload["m"]), bad, gpu_used(), base), flush=True)

    import torch.multiprocessing as mp    # NOT plain multiprocessing: torch.multiprocessing
    ctx = mp.get_context("spawn")          # is what registers the CUDA tensor reducers
    kids = []
    for port in ports:
        q = ctx.Queue()
        p = ctx.Process(target=ming_worker.main, args=(port, q), name="comfy-%d" % port)
        p.start()
        q.put(shared)                      # CUDA IPC handles, not a copy
        kids.append((port, p, q))
        print("spawned %d pid %d" % (port, p.pid), flush=True)
        time.sleep(1.0)

    print("serving %d tensors + %d quant subclass(es) (%.1f GB) to %d workers; gpu %d MiB"
          % (len(shared["t"]), len(shared["m"]),
             sum(t.numel() * t.element_size() for t in shared["t"].values()) / 1e9,
             len(kids), gpu_used()), flush=True)
    # Announce what is actually running so the spooler asks this pool for its own dtype and
    # its own ports instead of guessing from the settings file (see tools/tts_pool.py).
    tts_pool.announce(precision, [p for p, _, _ in kids])
    # Warm each worker once, before any real prompt can reach it. On this build a worker
    # binds the shared weight set and pays CUDA/graph setup plus the speaker-encoder and
    # clone-decode first-run work on its FIRST generation: ~19.8-22.1 s of dead air on the
    # user's first reply after a cold boot, versus 9.8 s when the worker has been warmed.
    # The warms run CONCURRENTLY now. They had to be sequential while a worker's first
    # generation made it pull its own dequantized weight copy onto the card - three fp32
    # copies of 4.8 GB each against a 24.5 GB card is how the pool used to fastfail. The
    # zero-copy bind removed that, and the numbers show how little a warm costs per worker:
    # 16084 -> 16313 -> 16560 -> 16671 MiB, about 0.15-0.25 GB of activations each.
    # DOW_WARM_SEQ=1 restores one-at-a-time if a future build reintroduces a private copy.
    if os.environ.get("DOW_WARM", "1") != "0":
        import threading
        if os.environ.get("DOW_WARM_SEQ") == "1":
            for port, p, q in kids:
                warm(port, MING_MODEL, precision)
        else:
            threads = [threading.Thread(target=warm, args=(port, MING_MODEL, precision),
                                        daemon=True) for port, p, q in kids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        print("warm-up done: gpu %d MiB, first reply should no longer wait on model bind"
              % gpu_used(), flush=True)
    print("pool ready: %s %d worker(s), precision=%s, gpu %d MiB"
          % (",".join(str(p) for p, _, _ in kids), len(kids), precision, gpu_used()), flush=True)
    try:
        while True:
            time.sleep(5)
            dead = [(pt, p.exitcode) for pt, p, _ in kids if p.exitcode is not None]
            if dead:
                print("worker died: %s" % dead, flush=True)
                for pt, p, _ in kids:
                    if p.exitcode is None:
                        p.terminate()
                break
    except KeyboardInterrupt:
        for pt, p, _ in kids:
            p.terminate()


if __name__ == "__main__":
    main()
