"""DoW-voice reply TTS helper for the dow-voice-chime desktop plugin.

Called by the plugin through the gateway's shell.exec, so stdout must stay tiny (<4000 chars).
  voices                                  -> JSON [{id,name,tag,ready}]
  say --voice <id> --b64 <base64(utf8)>   -> synthesize + play (blocking), print "ok <ms> <chars>"
  stop                                    -> kill the player of this voice stream

Synthesis prefers ComfyUI's resident Ming 3.0 TTS node (warm, cloning), and falls back to
audio.cpp CLI (cold model load each call). Voices live in HERMES_VOICES/<id>/:
  ref.wav + ref_text.txt + voice.json {name, tag, style, catch, swaps:{word:repl}}
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.request, uuid

# The shim and the sibling tools live next to this script (pythonNN._pth on the embedded
# interpreter leaves that directory off sys.path; the venv python finds it anyway).
_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import stack_paths as sp

HERMES = sp.HERMES_HOME
# HERMES_VOICES still wins (the env name the plugin era used); TTS_VOICES / tts_pack.json are
# the shim routes added by the port.
VOICES = os.environ.get("HERMES_VOICES") or sp.VOICES
COMFY = "http://127.0.0.1:8188"      # single-instance legacy `say`/synth path; the POOL ports
                                     # come from tts_pool: 8188 + configured count, unchanged
COMFY_IN = os.path.join(sp.COMFY_ROOT, "ComfyUI", "input")
COMFY_OUT = os.path.join(sp.COMFY_ROOT, "ComfyUI", "output")
MING_MODEL = "Ming-3.0-tts-0.5b-int8-convrot"   # folder NAME in the ComfyUI model registry, not
                                                # a filesystem path; stays one line because
                                                # ming_weightd._holder_env() regex-reads it
# audio.cpp fallback (the cold `say` path, not the pool): env > tts_pack.json > unconfigured.
# This box pins both in tts_pack.json, so Windows behaviour is byte-identical to the old
# hardcodes; unconfigured, the fallback raises a clear error instead of guessing a
# Windows-only absolute path on a foreign OS.
AUDIOCPP = sp.env("DOW_AUDIOCPP", "audiocpp_bin", None)
AUDIOCPP_MODEL = sp.env("DOW_AUDIOCPP_MODEL", "audiocpp_model", None)
TMP = sp.SCRATCH


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def jget(path, timeout=30):
    return json.loads(OPENER.open(COMFY + path, timeout=timeout).read())


def comfy_up():
    try:
        return bool(jget("/system_stats", 3))
    except Exception:
        return False


# --- shared-weight pool lifecycle -----------------------------------------------------
# tools/ming_weightd.py loads Ming once and spawns ComfyUI workers that map that single
# copy over CUDA IPC: 3 workers on one copy measured 41.1 s for the 6-sentence reply
# against 56.4 s on two independent streaming workers, with a lower in-job peak
# (16.8 vs 17.6 GB) and byte-identical audio. It parks ~4 GB of VRAM while up, so the
# plugin brings it up when a voice is chosen and tears it down on "off".
# Ports come from the pool settings (tts-voices/pool.json via tools/tts_pool.py), or from
# whatever a live pool announced; MING_WORKER_PORTS still wins for one-off experiments.
# (the sys.path insert at the top already makes tts_pool importable under python_embeded)
import tts_pool
# Ports for a pool we are about to START: the configured count wins. actual() is what a RUNNING
# pool announced, and using it here meant a restart inherited the previous pool's port set -
# measured: pool.json said 3 and the boot came up with 6 fp32 workers because the 6-worker pool
# being torn down had announced those ports. actual() belongs to the spooler's routing (which
# ports answer right now), not to boot decisions.
PORTS = [int(x) for x in os.environ.get("MING_WORKER_PORTS", "").split(",") if x.strip()] \
    or tts_pool.ports()
# Script-relative, NOT sp.TOOLS_DIR: on the deployed box the tools dir IS HERMES/tools, so the
# value is byte-identical to the old hardcode; run from a pack copy, it spawns THIS tree's
# holder instead of a deployed one, which is what a regression of this tree has to test.
HOLDER = os.path.join(_TOOLS, "ming_weightd.py")
PORTABLE = sp.COMFY_ROOT
PYEMBED = sp.python_in_comfy_root()
PIDFILE = os.path.join(TMP, "ming_weightd.pid")


def port_up(pt):
    try:
        OPENER.open("http://127.0.0.1:%d/system_stats" % pt, timeout=3).read()
        return True
    except Exception:
        return False


def pool_ready():
    return sum(1 for pt in PORTS if port_up(pt))


def _run_text(argv):
    """Run a helper and return its stdout as text. tasklist and PowerShell print UTF-16
    with a BOM on this box, which text=True (utf-8) chokes on mid-read in a reader thread,
    so decode by BOM and ignore anything else."""
    p = subprocess.run(argv, capture_output=True)
    raw = p.stdout
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "ignore")
    return raw.decode("utf-8", "ignore")


def pid_alive(pid):
    if sp.OS != "windows":
        try:                                  # POSIX: signal 0 is the process-exists probe
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError):
            return False
    return str(pid) in _run_text(["tasklist", "/FI", "PID eq %s" % pid, "/NH"])


def holder_pid():
    """The holder's pid: from our pid file, else by command line, because the pool may
    have been started by hand with tools/comfy_workers.ps1, which writes no pid file."""
    try:
        pid = int(open(PIDFILE).read().strip())
        if pid_alive(pid):
            return pid
    except Exception:
        pass
    found = sp.processes("ming_weightd")      # CIM CommandLine match on Windows (same source as
    return found[0][0] if found else 0        # the old inline ps), /proc cmdlines on Linux


def _audit(note):
    """One line in cache/scratch/pool_settings.log, the same file the `setting` trail uses.
    Kept separate from print() because a pool command's stdout often goes to a caller that
    does not keep it. Never fails the command it describes.

    Why: a teardown and a spontaneous death looked identical on 2026-11-08 (ports dead,
    `actual` cleared, holder gone, nothing new in weightd_err.log) and the plan recorded the
    05:10 loss as 'cause unknown' when the honest answer was 'we cannot tell'. ppid is the
    discriminator - a chip click arrives as a child of the gateway process, a hand or
    scripted boot arrives from whatever shell ran the tool."""
    try:
        with open(os.path.join(TMP, "pool_settings.log"), "a", encoding="utf8") as _fh:
            _fh.write("%s POOL ppid=%d %s\n" % (time.strftime("%H:%M:%S"), os.getppid(), note))
    except Exception:
        pass


def pool_up():
    n = pool_ready()
    if n == len(PORTS):
        print("up %d/%d" % (n, len(PORTS)))
        return
    pid = holder_pid()
    if pid and pid_alive(pid):
        print("spooling: holder %d already running, %d/%d ready" % (pid, n, len(PORTS)))
        return
    if tts_pool.settings()["precision"] == "fp32" and os.environ.get("MING_FP32_STREAM") == "1":
        # Escape hatch: the arrangement that was needed BEFORE the workers stopped loading
        # their own weights. fp32 on private per-worker copies peaked at 23.1 GB of a
        # 24.5 GB card; with the zero-copy bind (loader.py skips load_weights, unpack creates
        # the missing slots) the shared pool runs fp32 with 3 workers at 19.4 GB and no
        # crashes, so this branch should not be needed. Kept because it is the fallback if
        # comfy_kitchen ever regains a dtype that refuses IPC-mapped tensors.
        ports = PORTS[:2]
        for pt in ports:
            log = open(os.path.join(TMP, "stream_%d.log" % pt), "ab")
            subprocess.Popen([PYEMBED, "-s", "ComfyUI/main.py", "--port", str(pt)], cwd=PORTABLE,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             **sp.SPAWN_DETACH)   # was DETACHED|NEW_PROCESS_GROUP; the shim's
                                                  # constant also carries NO_WINDOW (invisible:
                                                  # stdout/stderr are files either way)
        open(PIDFILE, "w").write("")       # no holder to track in streaming mode
        print("spooling %d STREAMING workers (MING_FP32_STREAM=1); cold start ~60 s" % len(ports))
        return
    # Guard the boot, not just the chip click: pool_up also runs when a voice is selected, and
    # whatever count pool.json happens to hold may not fit at that moment - the engine may be
    # holding several GB, or another process has grown since the setting was made.
    # tts_pool.capacity() computes the limit from live GPU and commit availability against the
    # coefficients cap_probe.py measured; the static numbers only remain as a fallback when
    # nvidia-smi cannot be read.
    cap = tts_pool.capacity(tts_pool.settings()["precision"], reclaim_live=False)
    if len(PORTS) > cap["max"]:
        print("capping %d workers to %d: %s" % (len(PORTS), cap["max"], cap["arithmetic"]))
        import time
        try:                                  # this runs in a detached child whose stdout goes
            with open(os.path.join(TMP, "pool_settings.log"), "a", encoding="utf8") as _fh:
                _fh.write("%s pool_up capped %d -> %d | %s\n"
                          % (time.strftime("%H:%M:%S"), len(PORTS), cap["max"], cap["arithmetic"]))
        except Exception:
            pass
        PORTS[:] = PORTS[:cap["max"]]
        tts_pool.set_workers(cap["max"])   # pool.json is left holding what actually boots, so
    log = open(os.path.join(TMP, "weightd.log"), "ab")   # the chip highlights the real number
    err = open(os.path.join(TMP, "weightd_err.log"), "ab")
    env = dict(os.environ, MING_WORKER_PORTS=",".join(str(x) for x in PORTS))
    p = subprocess.Popen([PYEMBED, "-s", HOLDER], cwd=PORTABLE, env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=err,
                         **sp.SPAWN_DETACH)   # win: DETACHED|NEW_PROCESS_GROUP|NO_WINDOW; posix:
                                             # start_new_session. One spawn policy, in the shim.
    open(PIDFILE, "w").write(str(p.pid))
    print("spooling %d workers, holder %d (cold start takes ~60 s)" % (len(PORTS), p.pid))
    _audit("up booted holder=%d workers=%d precision=%s"
           % (p.pid, len(PORTS), tts_pool.settings()["precision"]))


def pool_down():
    """Kill the holder AND its tree. The workers are multiprocessing-spawn children whose
    IPC mappings die with the holder, so they must go with it - killing only the holder
    leaves them squatting on the ports with dead mappings, which the launcher learned the
    hard way. ExecutablePath is pinned to the portable python so an unrelated
    multiprocessing app is never touched."""
    victim = holder_pid()
    # Streaming workers (fp32 mode) are plain `ComfyUI/main.py --port N` processes: match our
    # own port list so a ComfyUI the user started themselves is never caught in the sweep.
    port_re = "|".join(str(p) for p in PORTS)
    if sp.OS == "windows":
        if victim:
            subprocess.run(["taskkill", "/PID", str(victim), "/T", "/F"], capture_output=True)
        ps = ("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" |"
          " Where-Object { $_.ExecutablePath -like '*ComfyUI_windows_portable*' -and"
          " ($_.CommandLine -match 'ming_weightd|multiprocessing\\.spawn|multiprocessing\\.popen'"
          " -or $_.CommandLine -match 'ComfyUI/main\\.py.*--port (" + port_re + ")\\b') } |"
          " ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
        out = _run_text(["powershell.exe", "-NoProfile", "-Command", ps]).split()
    else:
        # POSIX: the same target set as the Windows sweep, matched through /proc cmdlines
        # (sp.processes): our holder, its multiprocessing children (the spawn bootstraps - the
        # ps1 lesson), and streaming workers on OUR ports only; never a ComfyUI the user started
        # themselves. SIGKILL stands in for /F. The ExecutablePath pin has no /proc equivalent;
        # the port pin already bounds the damage.
        import signal as _signal
        out = []
        for pid, _cmd in sp.processes("ming_weightd|multiprocessing\\.spawn|multiprocessing\\.popen"
                                      "|ComfyUI/main\\.py.*--port (" + port_re + ")\\b"):
            try:
                os.kill(pid, _signal.SIGKILL)
                out.append(str(pid))
            except OSError:
                pass
    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    tts_pool.announce(tts_pool.settings()["precision"], [])   # nothing is live; stop the spooler
                                                                # from trusting a dead pool's ports
    killed = ",".join(out) or (str(victim) if victim else "nothing")
    live = pool_ready()
    print("down: killed %s, %d/%d still answering" % (killed, live, len(PORTS)))
    _audit("down killed=%s still=%d/%d ports_wanted=%s holder_was=%s"
           % (killed, live, len(PORTS), ",".join(map(str, PORTS)), victim or "none"))


def load_voice(vid):
    d = os.path.join(VOICES, vid)
    ref = os.path.join(d, "ref.wav")
    if not os.path.isfile(ref):
        raise SystemExit("no ref.wav for voice " + vid)
    txt = os.path.join(d, "ref_text.txt")
    meta = {}
    mj = os.path.join(d, "voice.json")
    if os.path.isfile(mj):
        meta = json.load(open(mj, encoding="utf8"))
    rtext = open(txt, encoding="utf8").read().strip() if os.path.isfile(txt) else ""
    return d, ref, rtext, meta


SWAP_SAFE = re.compile(r"[A-Za-z']+")


def stylize(text, meta, first=True):
    """Character flavour without changing the factual content."""
    out = text
    for a, b in (meta.get("swaps") or {}).items():
        out = re.sub(r"\b%s\b" % re.escape(a), b, out, flags=re.I)
    catch = meta.get("catch") or ""
    if catch and first:
        out = catch + " " + out
    style = meta.get("style") or ""
    if style and first:
        out = out.rstrip(" .!?") + ". " + style
    return out


def synth_comfy(text, ref, rtext, lang):
    # copy the reference into ComfyUI's input tree once per voice
    rel = "dow_voices/" + os.path.basename(os.path.dirname(ref)) + ".wav"
    dst = os.path.join(COMFY_IN, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(ref):
        import shutil
        shutil.copyfile(ref, dst)
    cid = uuid.uuid4().hex[:8]
    wf = "hermes_dow/" + cid
    graph = {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": rel}},
        "2": {"class_type": "MingLoadModel", "inputs": {"model": MING_MODEL, "precision": "auto",
                                                        "device": "auto", "download_if_missing": False}},
        "3": {"class_type": "MingGenerate", "inputs": {
            "model": ["2", 0], "text": text, "task": "clone",
            "task.reference_audio": ["1", 0], "task.reference_text": rtext,
            "task.language": lang or "English",
            "seed": 7, "cfg": 2.0, "flow_steps": 10, "sigma": 0.25,
            "temperature": 0.0, "max_steps": 250, "trim": True}},
        "4": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["3", 0],
                                                            "filename_prefix": wf, "format": "flac"}},
    }
    body = json.dumps({"prompt": graph, "client_id": str(uuid.uuid4())}).encode()
    req = urllib.request.Request(COMFY + "/prompt", data=body, headers={"Content-Type": "application/json"})
    pid = json.loads(OPENER.open(req, timeout=60).read())["prompt_id"]
    t0 = time.time()
    while time.time() - t0 < 300:
        time.sleep(1.5)
        h = jget("/history/" + pid, 20)
        if pid in h:
            st = (h[pid].get("status") or {})
            if st.get("status_str") == "error":
                raise RuntimeError("comfy error: " + json.dumps(st.get("messages"))[:200])
            for o in (h[pid].get("outputs") or {}).values():
                for a in o.get("audio", []):
                    p = os.path.join(COMFY_OUT, a.get("subfolder", ""), a["filename"])
                    if os.path.isfile(p):
                        return p
            raise RuntimeError("comfy produced no audio")
    raise RuntimeError("comfy timeout")


def synth_audiocpp(text, ref, rtext, vid):
    if not AUDIOCPP or not AUDIOCPP_MODEL:
        raise RuntimeError("audio.cpp fallback not configured: set DOW_AUDIOCPP and "
                           "DOW_AUDIOCPP_MODEL (or tts_pack.json audiocpp_bin/audiocpp_model)")
    out = os.path.join(TMP, "dow_%s_%d.wav" % (vid, int(time.time())))
    cmd = [AUDIOCPP, "--task", "tts", "--family", "audio8_tts", "--model", AUDIOCPP_MODEL,
           "--backend", "vulkan", "--voice-ref", ref.replace("\\", "/")]
    if rtext:
        cmd += ["--reference-text", rtext]
    cmd += ["--text", text, "--out", out.replace("\\", "/")]
    subprocess.run(cmd, check=True, capture_output=True, timeout=420)
    if not os.path.isfile(out):
        raise RuntimeError("audio.cpp produced nothing")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("arg2", nargs="?")            # pool: up | down | status
    ap.add_argument("--voice")
    ap.add_argument("--b64")
    ap.add_argument("--precision", choices=list(tts_pool.CHOICES))
    ap.add_argument("--workers", type=int)
    ap.add_argument("--flavor", help="on|off: character catch + persona sentence in replies")
    ap.add_argument("--first", action="store_true")
    a = ap.parse_args()

    if a.cmd == "voices":
        rows = []
        if os.path.isdir(VOICES):
            for vid in sorted(os.listdir(VOICES)):
                d = os.path.join(VOICES, vid)
                # Only directories are voices. The pool's settings file and the generation token
                # deliberately live in this same folder so the chip can reach them in one call,
                # and without this filter the menu grew a "pool.json"/POO row and a
                # ".generation"/.GE row in front of everything.
                if vid.startswith(".") or not os.path.isdir(d):
                    continue
                mj = os.path.join(d, "voice.json")
                meta = json.load(open(mj, encoding="utf8")) if os.path.isfile(mj) else {}
                rows.append({"id": vid, "name": meta.get("name", vid),
                             "tag": meta.get("tag", vid[:3].upper()),
                             "ready": os.path.isfile(os.path.join(d, "ref.wav"))})
        print(json.dumps(rows))
        return

    if a.cmd == "settings":
        s = tts_pool.settings()
        # The chip reads this one call to draw the whole Engine row, including which worker
        # counts fit. capacity() here uses reclaim_live=True (the default): the running pool is
        # about to be replaced by whatever is clicked, so its footprint is headroom the new
        # pool gets back - not a cost to charge twice.
        print(json.dumps(dict(s, live=tts_pool.actual() or None, urls=tts_pool.urls(),
                              capacity=tts_pool.capacity(s["precision"]))))
        return

    if a.cmd == "setting":
        # Audit trail, because "the UI setting does nothing" has two opposite causes - the
        # click never reached this process, or it did and the pool side is at fault - and
        # they are indistinguishable from the outside. Every settings change records argv,
        # the calling process id and the resulting port set to cache/scratch/pool_settings.log.
        try:
            import time
            with open(os.path.join(TMP, "pool_settings.log"), "a", encoding="utf8") as _fh:
                _fh.write("%s SETTING argv=%r ppid=%d\n"
                          % (time.strftime("%H:%M:%S"), sys.argv[1:], os.getppid()))
        except Exception:
            pass
        changed, s = [], tts_pool.settings()
        capnote = ""                           # carried into the last stdout line: the chip
                                               # reports only that line (see setSetting in the
                                               # plugin), so the arithmetic has to ride on it.
        if a.precision:
            s = tts_pool.set_precision(a.precision)
            changed.append("precision=" + s["precision"])
        if a.workers:
            # Clamp instead of silently honouring. fp32 at 3 workers measured fine (19.4 GB
            # peak, no deaths); 5 and 7 were clicked and those boots died - workers fastfailing
            # with 0xC0000409, plus boots overlapping each other. The reason goes into the
            # changed-list, NOT a separate print: the chip reports only the last stdout line,
            # so a note printed earlier is invisible to the user.
            want = a.workers
            # Do NOT clamp against live capacity here. At this instant the OLD pool is still
            # resident and charging ~4.3 GB of commit per worker, which makes the machine look
            # far fuller than it will be the moment the new one boots - measured: that misread
            # clamped a request for 3 fp32 workers down to 1. The clamp belongs in pool_up,
            # after teardown, where the availability it reads is the one the new pool will
            # actually get. The requested number is stored as asked; pool_up adjusts it.
            s = tts_pool.set_workers(want)
            changed.append("workers=%d (boot applies what fits at that moment)" % want)
        if a.flavor is not None:
            s = tts_pool.set_flavor(a.flavor)
            changed.append("flavor=%s" % ("on" if s["flavor"] else "off"))
        if changed and all(c.startswith("flavor") for c in changed):
            # Read per reply by the spooler's stylize(), so it must NOT restart the pool: a
            # restart costs ~60 s of warm boot for something that applies to the next sentence.
            print("set " + " ".join(changed) + " \u2014 applies to the next reply, no restart needed")
            return
        if not changed:
            print("want precision=%s workers=%d ports=%s | live %s"
                  % (s["precision"], s["workers"], ",".join(map(str, tts_pool.ports())),
                     tts_pool.actual() or "nothing announced"))
            return
        PORTS[:] = tts_pool.ports()
        print("set " + " ".join(changed) + " — restart scheduled, pool back in ~1 min "
              "(warm-up included)" + ((" | " + capnote) if capnote else ""))
        try:
            with open(os.path.join(TMP, "pool_settings.log"), "a", encoding="utf8") as _fh:
                _fh.write("   -> wants precision=%s workers=%s ports=%s (restart spawned detached)\n"
                          % (s["precision"], s["workers"], ",".join(map(str, tts_pool.ports()))))
        except Exception:
            pass
        # The restart must NOT happen inside this call. The desktop's shell.exec gives up at
        # 30 s while a settle + boot takes 60-90 s, so the plugin saw a rejection, never got
        # the reason line, and never refreshed - which is how a working feature looked dead.
        # Same trick as the spooler's `start`: spawn detached, return now.
        subprocess.Popen([sys.executable, sys.argv[0], "pool", "restart"],
                         cwd=_TOOLS,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         **sp.SPAWN_DETACH)
        return

    if a.cmd == "pool":
        act = (a.arg2 or "status").lower()
        if act in ("up", "down", "restart"):
            # The chip's on/off switch lands here as `pool down`, and a spontaneous death
            # produces the same outside picture (ports dead, announcement cleared). One line
            # per STATE-CHANGING command, with the caller's pid: the chip's calls arrive from
            # the gateway process, a hand/scripted boot from whatever shell ran the tool.
            # `status` is deliberately not logged - the chip polls it, and the trail would
            # fill with polls instead of events.
            _audit("cmd=%s argv=%r" % (act, sys.argv[1:]))
        if act == "up":
            pool_up()
        elif act == "down":
            pool_down()
        elif act == "restart":
            # Runs in a detached child (see the `setting` command): a settle plus a warm boot
            # takes longer than the desktop's 30 s shell.exec limit, so it must not block a
            # call the UI is waiting on. The settle is what stops a second holder from loading
            # its weight set on top of one that is still tearing down.
            pool_down()
            import time
            for _ in range(30):
                if holder_pid() is None:
                    break
                time.sleep(1)
            # GPU and commit keep draining for seconds after the last process is gone (worker
            # teardown, driver reclaim, CUDA IPC unmapping). Reading capacity during that window
            # is what capped a request for 7 workers down to ONE, because the card still showed
            # 6.7 GB free from a pool that was dying. Wait until usage stops falling: three
            # consecutive samples within 0.05 GB of each other.
            prev, stable = None, 0
            for _ in range(25):
                now = tts_pool._gpu_gb()
                if now and prev and abs(prev[1] - now[1]) < 0.05:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                prev = now
                time.sleep(1)
            time.sleep(2)
            pool_up()
            try:
                with open(os.path.join(TMP, "pool_settings.log"), "a", encoding="utf8") as _fh:
                    _fh.write("   restart done: ports=%s holder=%s\n"
                              % (",".join(map(str, PORTS)), holder_pid()))
            except Exception:
                pass
        else:
            print("up %d/%d, holder %s" % (pool_ready(), len(PORTS), holder_pid() or "none"))
        return

    if a.cmd == "stop":
        if sp.OS == "windows":
            subprocess.run(["taskkill", "/IM", "dow_tts.py", "/F"], capture_output=True)
        else:
            # POSIX: SIGTERM every other dow_tts.py process; the detached `play` owner stops
            # its player child in its SIGTERM handler's finally. (/IM matches an exe name and
            # never matched this script name on Windows; kept untouched for parity.)
            import signal as _signal
            for pid, _cmd in sp.processes("dow_tts\\.py"):
                if pid != os.getpid():
                    try:
                        os.kill(pid, _signal.SIGTERM)
                    except OSError:
                        pass
        return

    if a.cmd == "say":
        d, ref, rtext, meta = load_voice(a.voice)
        text = base64.b64decode(a.b64).decode("utf8", "ignore").strip()
        if not text:
            print("ok 0 0")
            return
        styled = stylize(text, meta, bool(a.first))
        lang = meta.get("language", "English")
        t0 = time.time()
        try:
            wav = synth_comfy(styled, ref, rtext, lang) if comfy_up() else synth_audiocpp(styled, ref, rtext, a.voice)
        except Exception as e:
            wav = synth_audiocpp(styled, ref, rtext, a.voice) if comfy_up() else None
            if not wav:
                print("ERR %s" % str(e)[:120])
                return
        ms = int((time.time() - t0) * 1000)
        wavp = wav.replace("\\", "/")
        try:  # detached player so the gateway shell.exec returns at once
            subprocess.Popen([sys.executable, sys.argv[0], "play", "--voice", wavp],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             **sp.SPAWN_DETACH)
        except Exception:
            pass
        print("ok %d %s" % (ms, wavp))
        return

    if a.cmd == "play":   # --voice carries the wav path here
        # Windows: sp.play_wav is exactly the old blocking PlaySound(SND_FILENAME) - this
        # process was spawned detached by `say`, so the gateway call already returned.
        # POSIX: play_wav returns the player child; this process is its single owner until
        # the clip ends, and `stop` (SIGTERM) takes both down via the handler's finally.
        h = sp.play_wav(a.voice, interruptible=False)
        if h is not None:
            import signal
            signal.signal(signal.SIGTERM, lambda *_s: sys.exit(0))
            try:
                h.wait()
            finally:
                if h.poll() is None:
                    sp.stop_play(h)
        print("ok")
        return

    print("unknown cmd")


main()
