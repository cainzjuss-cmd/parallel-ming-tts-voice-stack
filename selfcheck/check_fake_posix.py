#!/usr/bin/env python3
"""Fake-POSIX import check for the ported TTS tools (runs on the Windows box).

For every module in ../files/, a fresh subprocess:
  - fakes os.name="posix" / sys.platform="linux" BEFORE stack_paths is first imported
    (the shim derives sp.OS from these, so the windows branches must never be entered),
  - poisons winsound (sys.modules=None => any import winsound raises),
  - poisons ctypes.windll / ctypes.WinDLL / ctypes.oledll (any touch raises),
  - pins every state root (HERMES_HOME, TTS_SCRATCH, TTS_VOICES, TTS_TOOLS, COMFY_ROOT,
    MING_SHARE_LOG) to selfcheck/fakehome so the check can never touch the live tree,
  - imports the module with a harmless argv (dow_tts/dow_spool still run their main() at
    import, exactly as before the port - "unknown cmd" is the safe no-op command).

Pass = exit 0 and the shim reported sp.OS == "linux". This proves no import-time Windows
touch on a POSIX platform; it does NOT prove runtime correctness on Linux (no Linux box
here) - see docs/PORT_STATUS.md for what was not verified.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(os.path.dirname(HERE), "files")
FAKE = os.path.join(HERE, "fakehome")
os.makedirs(os.path.join(FAKE, "cache", "scratch"), exist_ok=True)
os.makedirs(os.path.join(FAKE, "tts-voices"), exist_ok=True)
os.makedirs(os.path.join(FAKE, "ComfyUI", "ComfyUI"), exist_ok=True)

MODS = ["stack_paths", "tts_pool", "dow_tts", "dow_spool", "ming_worker", "ming_weightd",
        "cap_probe", "mem_trim", "build_dow_voices", "apply_share_hook"]

PREWARM = '''
import argparse, base64, ctypes, io, json, os, re, shutil, signal, subprocess, sys
import tempfile, threading, time, urllib.request, urllib.error, uuid, runpy
'''

HARNESS = PREWARM + r'''
os.environ["HERMES_HOME"] = os.environ["FAKE_HOME"]
_scratch = os.path.join(os.environ["FAKE_HOME"], "cache", "scratch")
os.environ["TTS_SCRATCH"] = _scratch
os.environ["TTS_VOICES"] = os.path.join(os.environ["FAKE_HOME"], "tts-voices")
os.environ["TTS_TOOLS"] = os.environ["FAKE_HOME"] + os.sep + "tools"
os.environ["COMFY_ROOT"] = os.path.join(os.environ["FAKE_HOME"], "ComfyUI")
os.environ["MING_SHARE_LOG"] = os.path.join(_scratch, "share_path.log")
os.environ["MING_WORKER_PORTS"] = ""

# --- platform fakes AFTER prewarm, BEFORE stack_paths is imported ---
sys.modules["winsound"] = None                      # any `import winsound` -> ImportError
def _boom(*a, **k):
    raise AssertionError("Windows API touched on a fake-POSIX import: %r" % (a,))
ctypes.windll = None
ctypes.oledll = None
ctypes.WinDLL = _boom
ctypes.WINFUNCTYPE = _boom
os.name = "posix"
sys.platform = "linux"

sys.argv = [os.environ["TARGET_MOD"] + ".py", "no-such-cmd"]  # dow_tts/dow_spool call main()
sys.path.insert(0, os.environ["TARGET_DIR"])                  # at import, as before the port
name = os.environ["TARGET_MOD"]
m = __import__(name)
import stack_paths as sp
assert sp.OS == "linux", "shim did not resolve to linux: %r" % (sp.OS,)
assert "start_new_session" in sp.SPAWN_DETACH, sp.SPAWN_DETACH
print("IMPORT-OK  OS=%s spawn=%s" % (sp.OS, sorted(sp.SPAWN_DETACH)))
'''

RUN = PREWARM + r'''
os.environ["HERMES_HOME"] = os.environ["FAKE_HOME"]
os.environ["TTS_SCRATCH"] = os.path.join(os.environ["FAKE_HOME"], "cache", "scratch")
os.environ["TTS_VOICES"] = os.path.join(os.environ["FAKE_HOME"], "tts-voices")
os.environ["COMFY_ROOT"] = os.path.join(os.environ["FAKE_HOME"], "ComfyUI")
os.name = "posix"
sys.platform = "linux"
sys.path.insert(0, os.environ["TARGET_DIR"])
# the -c harness ate argv[0]; rebuild mem_trim's own argv from the remaining flags
sys.argv = ["mem_trim.py"] + sys.argv[1:]
runpy.run_path(os.path.join(os.environ["TARGET_DIR"], "mem_trim.py"), run_name="__main__")
print("RUN-OK mem_trim no-op path, rc=0")
'''

fails = []
for mod in MODS:
    env = dict(os.environ, FAKE_HOME=FAKE, TARGET_DIR=FILES, TARGET_MOD=mod)
    r = subprocess.run([sys.executable, "-c", HARNESS], env=env,
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip() or ((r.stderr or "").strip().splitlines() or [""])[-1]
    print("%-18s rc=%d %s" % (mod, r.returncode, out))
    if r.returncode != 0:
        fails.append((mod, r.stderr))

# mem_trim must also RUN (as __main__) into its documented no-op, exit 0
env = dict(os.environ, FAKE_HOME=FAKE, TARGET_DIR=FILES)
r = subprocess.run([sys.executable, "-c", RUN, "--proc", "strata", "--needed", "100"],
                   env=env, capture_output=True, text=True, timeout=120)
print("%-18s rc=%d %s" % ("mem_trim RUN", r.returncode, (r.stdout or r.stderr).strip()))
if r.returncode != 0:
    fails.append(("mem_trim RUN", r.stderr))

for mod, err in fails:
    print("\n---- %s stderr ----\n%s" % (mod, err))
print("\nRESULT:", "ALL IMPORTS CLEAN UNDER FAKE POSIX" if not fails else "FAILURES: %s" % [f[0] for f in fails])
sys.exit(1 if fails else 0)
