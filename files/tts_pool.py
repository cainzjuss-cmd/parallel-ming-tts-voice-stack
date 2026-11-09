"""Pool settings for the Ming TTS reply voice, and the two-file reason they need care.

tts-voices/pool.json                the SETTINGS the user picked in the desktop chip:
                                    {"precision": "auto|bf16|fp32", "workers": 1..7}
cache/scratch/ming_weightd.actual   what the RUNNING shared-weight pool actually is:
                                    {"precision": ..., "ports": [8188, ...]}

Two files, because both settings are frozen when the holder loads the weights and spawns its
children. Asking a live pool for a dtype it does not have gets rejected by the strict dtype
check in ming_worker.unpack(), and asking for a port it never opened just fails the item -
and both failures look identical at the user end: 0 items, no error text. So the spooler asks
the pool what it is running and generates with that; the settings file only decides what the
NEXT pool start looks like. Changing either value has to restart the pool, which the helper
subcommand in dow_tts.py does in one step.
"""
import json, os, sys, time

# The shim and the sibling tools sit next to this file; pythonNN._pth on the embedded
# interpreter leaves that directory off sys.path in spawned children, so make the sibling
# import explicit here instead of trusting whoever imported us.
_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import stack_paths as sp

HERMES = sp.HERMES_HOME    # kept as an attribute: ming_weightd used tts_pool.HERMES once
SETTINGS = os.path.join(sp.VOICES, "pool.json")
ACTUAL = os.path.join(sp.SCRATCH, "ming_weightd.actual")
CHOICES = ("auto", "bf16", "fp32")
BASE_PORT = 8188
MIN_WORKERS, MAX_WORKERS = 1, 7
DEFAULTS = {"precision": "auto", "workers": 3, "flavor": True}   # auto resolves to bf16 on this
                                                  # GPU; 3 workers on one shared weight copy
                                                  # measured 40.1 s vs 56.4 s for two streaming
                                                  # workers. `flavor` gates the character catch
                                                  # (prepended) and the persona sentence
                                                  # (appended) on the first chunk of a spoken
                                                  # reply. It does NOT gate the vocabulary swaps
                                                  # or the per-voice atempo - those are the
                                                  # accent, not flavour text.


def _read(path):
    try:
        with open(path, encoding="utf8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    return data


def settings():
    """User settings, clamped, with DOW_PRECISION honoured for one-off experiments."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in _read(SETTINGS).items() if k in DEFAULTS})
    if os.environ.get("DOW_PRECISION") in CHOICES:
        s["precision"] = os.environ["DOW_PRECISION"]
    if s["precision"] not in CHOICES:
        s["precision"] = DEFAULTS["precision"]
    try:
        n = int(s["workers"])
    except (TypeError, ValueError):
        n = DEFAULTS["workers"]
    s["workers"] = max(MIN_WORKERS, min(MAX_WORKERS, n))
    return s


def set_precision(value):
    s = settings()
    s["precision"] = value if value in CHOICES else DEFAULTS["precision"]
    _write(SETTINGS, s)
    return s


def set_workers(n):
    s = settings()
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULTS["workers"]
    s["workers"] = max(MIN_WORKERS, min(MAX_WORKERS, n))
    _write(SETTINGS, s)
    return s


def set_flavor(value):
    """On/off for the character catch and the persona sentence in spoken replies. Anything that
    is not an explicit off-word stays ON, so a typo from the chip cannot silently mute the
    persona layer - the failure mode would be invisible to the user."""
    s = settings()
    off_words = (False, 0, "0", "off", "no", "false", "none", None)
    s["flavor"] = not (value in off_words)
    _write(SETTINGS, s)
    return s


def ports():
    return [BASE_PORT + i for i in range(settings()["workers"])]


# THESE COEFFICIENTS AND MARGINS ARE WINDOWS-MEASURED CONSTANTS, corrected twice with the
# operator and agreed as-is: do NOT re-tune or re-derive them in a refactor. On Linux the same
# names mean something different (Windows charges private commit against the pagefile limit;
# Linux commit follows the overcommit policy - see sp.mem_avail). The shipped numbers must be
# RE-MEASURED there with cap_probe.py before the guard is trusted on Linux.
# Coefficients MEASURED with tools/cap_probe.py (2026-11-07, eight configs: bf16 1-5 workers,
# fp32 1-3, same 7-chunk reply, warm pool, tight 0.4 s GPU polling):
#   weights on the card (ONE shared copy): 2.6 GB bf16, 4.8 GB fp32
#   whole-batch card delta: +176..252 MiB bf16, +405..453 MiB fp32, essentially independent of
#     worker count, since weights are shared and the queue rarely decodes on every worker at once
#   private commit: holder 3.3 GB bf16 / 3.9 GB fp32 at idle; each worker 4.1-4.4 GB, idle or
#     mid-job (this is the axis the old fp32 multi-worker boots died on, not VRAM)
# The planning numbers below are the UPPER bound of those measurements: the guard is a guard,
# not a target, and real cost usually sits below it.
COEF = {
    # worker_gpu was 0.25/0.50, which turned out to be ~6x conservative against the sweep: the
    # WHOLE batch card delta at 6 fp32 workers was +444 MiB, at 5 it was +480, at 4 +435 - i.e.
    # 0.07-0.11 GB per concurrent decode, not 0.50. The model is 0.5B and streams its decode, so
    # activations really are small; the old number came from the pre-sharing regime where each
    # worker parked its own private weight copy. These are measured marginal costs with roughly
    # a 2x safety factor on top, because the all-workers-decoding-at-once worst case has only
    # been observed up to 6, and only at one chunk length.
    "auto": {"weights": 2.6, "worker_gpu": 0.10, "worker_commit": 4.5, "holder_commit": 3.5},
    "bf16": {"weights": 2.6, "worker_gpu": 0.10, "worker_commit": 4.5, "holder_commit": 3.5},
    "fp32": {"weights": 4.8, "worker_gpu": 0.20, "worker_commit": 4.5, "holder_commit": 4.0},
}
GPU_MARGIN_GB = 1.5      # leave the card room for the desktop, Parsec, browsers
COMMIT_MARGIN_GB = 6.0   # leave the machine room for the engine and the OS
# Note on worker_commit: these are per-process private-commit figures read off individual workers
# while they were idle or mid-job (4.1-4.3 GB). Do NOT derive them by differencing the machine's
# total free commit before and after a boot: the engine's own commit changes in the same window
# (observed 18.9 -> 25.9 GB), so that arithmetic attributes the engine's growth to the pool.
# VRAM and commit are separate axes with different reliability: the GPU delta is readable because
# the card is quiet, the commit figure is not.
MAX_WORKERS = 7          # the chip offers 1-7
# NO hard per-precision ceiling: the measured limits below do the deciding. History behind
# that decision: a constant of 3 blocked your request for 7 even though the same calculation
# said 16 by VRAM and 6 by commit. The fp32 fastfails at 5 and 7 workers happened BEFORE the
# zero-copy bind (workers loading private dequantized copies) and before boots were serialised;
# after both, fp32 measured clean at 4 (+435 MiB batch, 0 deaths), 5 (+480, 0 deaths) and 6
# (+444, 7 items rendered, 0 deaths, 22.6 GB card used with the engine holding 23 GB of commit).
# The commit axis, not VRAM, is what binds on this box: ~4.5 GB per worker against whatever
# the engine and OS are holding.
# Only used when the live read below is unavailable (nvidia-smi missing or failing):
STATIC_FLOOR = {"auto": 3, "bf16": 3, "fp32": 3}


def _avail_gb():
    """(available physical RAM GB, available commit/pagefile GB) or None. Lives in the shim now
    (sp.mem_avail): GlobalMemoryStatusEx on Windows, CommitLimit - Committed_AS from /proc/meminfo
    on Linux. Commit, not RAM, is the axis the fp32 boots died on."""
    return sp.mem_avail()


def _gpu_gb():
    """(total, used) GB on the GPU, or None when nvidia-smi cannot be read. Shim primitive
    (sp.gpu_mem); dow_tts's restart settle loop calls this through us, so the name stays."""
    return sp.gpu_mem()


def capacity(precision=None, reclaim_live=True):
    """How many workers the machine can take, computed from live availability and the measured
    coefficients - no constant caps it. reclaim_live=True is what the CHIP uses: a settings
    change tears the running pool down before booting the new one, so the current pool's own
    footprint (shared weights + per-worker commit/activations) is headroom the new pool will
    get back, not a cost. Measuring against the pre-teardown numbers double-counts and made a
    running 6-worker fp32 pool look like it could barely fit one worker.
    reclaim_live=False is for pool_up, which is called AFTER the teardown has already happened.
    Returns dict(max, limit_gpu, limit_commit, free_gpu_gb, avail_commit_gb, arithmetic)."""
    p = precision or settings()["precision"]
    c = COEF.get(p) or COEF["auto"]
    gpu, mem = _gpu_gb(), _avail_gb()
    if gpu is None or mem is None:
        return {"max": STATIC_FLOOR.get(p, 3), "limit_gpu": None, "limit_commit": None,
                "free_gpu_gb": None, "used_gpu_gb": None, "total_gpu_gb": None,
                "avail_commit_gb": None, "coef": c,
                "arithmetic": "live availability unreadable - using the static floor of %d"
                              % STATIC_FLOOR.get(p, 3)}
    live = actual() if reclaim_live else {}
    lw = len(live.get("ports") or [])
    lp = (live.get("precision") or p)
    lc_coef = COEF.get(lp) or COEF["auto"]
    back_gpu = (lc_coef["weights"] + lw * lc_coef["worker_gpu"]) if lw else 0.0
    back_commit = (lc_coef["holder_commit"] + lw * lc_coef["worker_commit"]) if lw else 0.0
    free_gpu = (gpu[0] - gpu[1]) + back_gpu
    avail_commit = mem[1] + back_commit
    # VRAM: the pool pays the shared weight set once, then each concurrent decode adds its own
    # activations; whatever else is on the card is already inside `used`.
    lg = int((free_gpu - GPU_MARGIN_GB - c["weights"]) // c["worker_gpu"])
    # Commit: holder + N workers, with the OS and engine needing the margin on top.
    lc = int((avail_commit - COMMIT_MARGIN_GB - c["holder_commit"]) // c["worker_commit"])
    allowed = max(1, min(MAX_WORKERS, lg, lc))
    return {"max": allowed, "limit_gpu": lg, "limit_commit": lc,
            "live_workers": lw, "reclaimed_gpu_gb": round(back_gpu, 2),
            "reclaimed_commit_gb": round(back_commit, 2),
            "free_gpu_gb": round(free_gpu, 2), "used_gpu_gb": round(gpu[1], 2),
            "total_gpu_gb": round(gpu[0], 2), "avail_commit_gb": round(avail_commit, 2),
            "coef": c,
            "arithmetic": ("%s: gpu free %.1f of %.0f GB%s, minus %.1f margin, minus %.1f "
                           "weights = %.1f / %.2f per decode -> %d | commit free %.1f%s, minus "
                           "%.1f margin, minus %.1f holder = %.1f / %.2f per worker -> %d"
                           % (p, gpu[0] - gpu[1], gpu[0],
                              (" + %.1f reclaimed from the live %d-worker pool" % (back_gpu, lw))
                              if lw else "",
                              GPU_MARGIN_GB, c["weights"],
                              free_gpu - GPU_MARGIN_GB - c["weights"], c["worker_gpu"], lg,
                              mem[1],
                              (" + %.1f reclaimed" % back_commit) if lw else "",
                              COMMIT_MARGIN_GB, c["holder_commit"],
                              avail_commit - COMMIT_MARGIN_GB - c["holder_commit"],
                              c["worker_commit"], lc))}


def actual():
    """What a live pool announced, or {} when nothing has (a dead pool's file is tolerated:
    the spooler probes the ports anyway and falls back)."""
    a = _read(ACTUAL)
    try:
        if time.time() - os.path.getmtime(ACTUAL) > 86400:
            return {}
    except OSError:
        return {}
    if a.get("ports") and a.get("precision") in CHOICES:
        return a
    return {}


def announce(precision, port_list):
    return _write(ACTUAL, {"precision": precision, "ports": list(port_list)})


def graph_precision():
    """What MingLoadModel should be asked for: the live pool's dtype if there is one."""
    return actual().get("precision") or settings()["precision"]


def urls(env_var="DOW_COMFY"):
    """Worker endpoints for the spooler: the live pool's ports when it announced them,
    otherwise the configured count. An explicit DOW_COMFY always wins, for experiments."""
    env = os.environ.get(env_var)
    if env:
        return [u.strip() for u in env.split(",") if u.strip()]
    return ["http://127.0.0.1:%d" % p for p in (actual().get("ports") or ports())]
