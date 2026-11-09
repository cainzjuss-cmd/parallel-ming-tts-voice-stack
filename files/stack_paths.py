"""One place for everything that differs between machines and operating systems.

The TTS stack (dow_tts, dow_spool, tts_pool, ming_weightd, ming_worker, cap_probe) used to
hardcode Windows paths, call windll directly, and spawn with Windows creationflags. This shim
replaces all of that so the same source tree runs on Windows and Linux. Nothing here is clever:
it is a lookup table over real platform differences, each with a comment saying why the
difference exists.

Resolution order for every path: explicit environment variable, then tts_pack.json next to this
file, then a per-OS default. That ordering is deliberate - the running pool and the spooler have
to agree about where state lives, and a stale guess in one process makes replies go silent.
"""
import json
import os
import subprocess
import sys

OS = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")


def _cfg_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("TTS_PACK_CONFIG"), os.path.join(here, "tts_pack.json")):
        if cand and os.path.isfile(cand):
            try:
                return json.load(open(cand, encoding="utf8"))
            except Exception:
                pass
    return {}


CFG = _cfg_file()


def _env(name, cfg_key, default):
    return os.environ.get(name) or CFG.get(cfg_key) or default


def env(name, cfg_key, default):
    """Public accessor for config keys that have no primitive here (the audio.cpp fallback
    binary/model, the voice-source tree). Same resolution order: env > tts_pack.json >
    default. Tools must not read CFG directly - one rule for every path, everywhere."""
    return _env(name, cfg_key, default)


HERMES_HOME = _env("HERMES_HOME", "hermes_home",
                   os.path.expandvars(r"%LOCALAPPDATA%\hermes") if OS == "windows"
                   else os.path.expanduser("~/.hermes"))
TOOLS_DIR = _env("TTS_TOOLS", "tools_dir", os.path.join(HERMES_HOME, "tools"))
SCRATCH = _env("TTS_SCRATCH", "scratch", os.path.join(HERMES_HOME, "cache", "scratch"))
VOICES = _env("TTS_VOICES", "voices", os.path.join(HERMES_HOME, "tts-voices"))
AB_DIR = _env("TTS_AB", "ab_dir", os.path.join(HERMES_HOME, "tts-ab"))
COMFY_ROOT = _env("COMFY_ROOT", "comfy_root",
                  r"C:\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable"
                  if OS == "windows" else os.path.expanduser("~/ComfyUI"))
MODEL_DIR = _env("MING_MODEL_DIR", "model_dir", os.path.join(COMFY_ROOT, "ComfyUI", "models",
                                                             "checkpoints", "ming-tts"))
SHARE_LOG = _env("MING_SHARE_LOG", "share_log", os.path.join(SCRATCH, "share_path.log"))
os.makedirs(SCRATCH, exist_ok=True)

# Spawning a detached child that must OUTLIVE the call that started it: the desktop's shell.exec
# kills its process tree when the call returns, and a pool worker spawned inside that tree dies
# with it, which looks exactly like "the setting did nothing". DETACHED_PROCESS(0x8) plus
# NEW_PROCESS_GROUP(0x200) plus CREATE_NO_WINDOW(0x08000000) is the Windows answer; on POSIX the
# equivalent is a new session.
if OS == "windows":
    SPAWN_DETACH = {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}
else:
    SPAWN_DETACH = {"start_new_session": True}


def python_in_comfy_root():
    """Interpreter inside the ComfyUI install: embedded portable on Windows, venv on Linux."""
    if OS == "windows":
        return os.path.join(COMFY_ROOT, "python_embeded", "python.exe")
    for cand in (os.path.join(COMFY_ROOT, "venv", "bin", "python"),
                 os.path.join(COMFY_ROOT, ".venv", "bin", "python")):
        if os.path.isfile(cand):
            return cand
    return sys.executable


def mem_avail():
    """(available physical GB, available COMMIT GB) or None.

    Commit, not RAM, is the axis this stack dies on: a huge file-backed mapping is reclaimable,
    while private allocations charge the commit limit whether resident or not. Windows exposes it
    as ullAvailPageFile; Linux exposes CommitLimit - Committed_AS in /proc/meminfo (overcommit
    mode 0 makes that a real hard limit; mode 2 heuristic differs - documented, not hidden).
    """
    if OS == "windows":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return (m.ullAvailPhys / 1e9, m.ullAvailPageFile / 1e9)
    try:
        info = {}
        with open("/proc/meminfo", encoding="utf8") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])           # kB
        commit = (info["CommitLimit"] - info["Committed_AS"]) * 1024.0 / 1e9
        return (info["MemAvailable"] * 1024.0 / 1e9, commit)
    except Exception:
        return None


def gpu_mem():
    """(total, used) GB, or None when nvidia-smi is missing or fails."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=10).stdout.strip().splitlines()
        total, used = [float(x) for x in out[0].split(",")[:2]]
        return (total / 1024.0, used / 1024.0)
    except Exception:
        return None


def processes(pattern):
    """[(pid, cmdline)] whose command line matches `pattern`, via platform-native sources."""
    out = []
    if OS == "windows":
        ps = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object "
             "{ $_.CommandLine -match '%s' } | ForEach-Object { '{0} {1}' -f $_.ProcessId, "
             "$_.CommandLine }" % pattern.replace("'", "")],
            capture_output=True, text=True).stdout
        for line in ps.splitlines():
            pid, _, rest = line.strip().partition(" ")
            if pid.isdigit():
                out.append((int(pid), rest))
        return out
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % entry, "rb") as fh:
                    cmd = fh.read().replace(b"\0", b" ").decode("utf8", "replace")
            except Exception:
                continue
            import re as _re
            if _re.search(pattern, cmd):
                out.append((int(entry), cmd))
            elif "python" in cmd and _re.search(pattern, cmd.replace("'", "")):
                out.append((int(entry), cmd))
    except Exception:
        pass
    return out


def priv_gb(pid):
    """Private commit of a process in GB: PagefileUsage on Windows, Private_Dirty on Linux."""
    if OS == "windows":
        import ctypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        p = PMC()
        p.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not h:
            return -1.0
        try:
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(p), p.cb)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
        return p.PagefileUsage / 1e9 if ok else -1.0
    try:
        with open("/proc/%d/smaps_rollup" % pid, encoding="utf8") as fh:
            for line in fh:
                if line.startswith("Private_Dirty:"):
                    return int(line.split()[1]) * 1024.0 / 1e9
    except Exception:
        return -1.0
    return -1.0


# Playback and barge-in. The ordered playlist (one player, strict in-order gate, 280 ms gap) is
# deliberately identical on both platforms; only the mechanics of "play this wav" and "stop the
# word being spoken now" differ. winsound has no Linux equivalent, and SND_PURGE has no POSIX
# equivalent, so the Linux path tracks the child pid and terminates it.
def play_wav(path, interruptible=True):
    if OS == "windows":
        import winsound
        # PlaySound, not PlayFile (winsound has no PlayFile - fixed during port rewiring):
        # SND_ASYNC is what lets the spooler poll the barge-in token while the item is still
        # sounding; interruptible=False is dow_tts's blocking `play`, byte-identical to the
        # old inline winsound.PlaySound(path, SND_FILENAME) call it replaces.
        winsound.PlaySound(path, winsound.SND_FILENAME | (winsound.SND_ASYNC if interruptible else 0))
        return None
    import signal
    import time
    for player in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                   ["pw-play", path], ["paplay", path]):
        try:
            p = subprocess.Popen(player, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            continue
        return p
    return None


def stop_play(handle):
    if OS == "windows":
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return
    if handle is None:
        return
    try:
        import signal
        handle.send_signal(signal.SIGTERM)
    except Exception:
        pass


def player_backend():
    if OS == "windows":
        return "winsound"
    for exe in ("ffplay", "pw-play", "paplay"):
        if subprocess.run(["which", exe], capture_output=True).returncode == 0:
            return exe
    return None
