#!/usr/bin/env python3
"""Measure what one Ming TTS worker actually costs, per precision, so the worker-count guard
can predict from live availability instead of a guessed constant.

Run as:  python cap_probe.py <precision>:<workers> [<precision>:<workers> ...]
e.g.     python cap_probe.py auto:3 auto:4 fp32:2

For each config it:
  1. applies the setting (the helper restarts the pool detached, so this returns fast),
  2. waits for the holder's "pool ready" line,
  3. samples GPU used plus holder/worker private commit at idle,
  4. runs a bench whose chunks land on different workers at once (so all decode together)
     while polling GPU at 0.4 s, which is what the slow 3 s samplers used to miss,
  5. appends a row to cap_measure.txt (durable, so a lost terminal does not lose the data).

Per-worker GPU cost is derived as (gpu_peak - gpu_idle) / workers_actually_decoding, and the
bench's own item count is recorded so a config where workers died is obvious rather than
looking like a cheap win.

On Linux this script is the tool that must RE-MEASURE the Windows-box coefficients baked into
tts_pool.COEF: the shipped figures are pagefile-commit numbers from Windows, and Linux commit
accounting follows the overcommit policy (sp.mem_avail maps CommitLimit - Committed_AS).
Never port the constants without running the sweep here first.
"""
import os
import re
import subprocess
import sys
import threading
import time

# script dir first: the shim and the sibling tools live next to this file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack_paths as sp

HERMES = sp.HERMES_HOME
TOOLS = os.path.dirname(os.path.abspath(__file__))
SCRATCH = sp.SCRATCH
# VP runs dow_tts.py (settings + detached pool restart): the hermes-agent venv lives under
# Scripts/ on Windows, under bin/ in a Linux venv - the computed default equals the old
# hardcode on this box. DOW_VENV_PY / tts_pack.json "venv_python" cover odd layouts.
VP = sp.env("DOW_VENV_PY", "venv_python",
            os.path.join(HERMES, "hermes-agent", "venv",
                         "Scripts" if sp.OS == "windows" else "bin",
                         "python.exe" if sp.OS == "windows" else "python"))
PYEMBED = sp.python_in_comfy_root()
DOW_TTS = os.path.join(TOOLS, "dow_tts.py")
BENCH = os.path.join(SCRATCH, "bench3.py")
TEXT = os.path.join(TOOLS, "sents_test.txt")
HOLDER_LOG = os.path.join(SCRATCH, "weightd.log")
OUT = os.path.join(SCRATCH, "cap_measure.txt")


def gpu_used():
    """MiB used on the card, via the shim's nvidia-smi read (sp.gpu_mem). nvidia-smi answers
    whole MiB, so the GB round-trip inside gpu_mem() stays exact here; failure is -1 as before."""
    g = sp.gpu_mem()
    return int(round(g[1] * 1024.0)) if g else -1


def _procs():
    """(pid, is_holder) for every ming_weightd holder and its IPC-spawned workers.
    sp.processes reads Get-CimInstance Win32_Process on Windows - the same source as the old
    inline powershell - and scans /proc/*/cmdline on Linux; H/W classification by whether the
    command line names the holder is identical."""
    out = []
    for pid, cmd in sp.processes("ming_weightd|spawn_main"):
        out.append((pid, "H" if "ming_weightd" in cmd else "W"))
    return out


def priv_gb(pid):
    """Private commit in GB - PagefileUsage on Windows, Private_Dirty on Linux (sp.priv_gb).
    The number tied to the commit-exhaustion class."""
    return sp.priv_gb(pid)


def main():
    configs = sys.argv[1:] or ["auto:3"]
    for cfg in configs:
        precision, workers = cfg.split(":")
        workers = int(workers)
        subprocess.run([VP, "-s", DOW_TTS, "setting", "--precision", precision,
                        "--workers", str(workers)], capture_output=True, text=True)
        off = os.path.getsize(HOLDER_LOG)
        ready = False
        for _ in range(150):
            with open(HOLDER_LOG, "rb") as fh:
                fh.seek(off)
                if b"pool ready" in fh.read():
                    ready = True
                    break
            time.sleep(1)
        if not ready:
            row = "%-5s %d  BOOT FAILED - no pool ready within 150s" % (precision, workers)
            print(row, flush=True)
            with open(OUT, "a", encoding="utf8") as fh:
                fh.write(row + "\n")
            continue
        time.sleep(3)
        idle_gpu = gpu_used()
        procs = _procs()
        idle_priv = {pid: priv_gb(pid) for pid, _ in procs}
        env = dict(os.environ, DOW_NOPLAY="1", DOW_DEBUG="1")
        samples = [idle_gpu]
        stop = threading.Event()

        def poll():                        # the bench's own summary is the reference; this
            while not stop.is_set():       # thread exists to catch peaks between those polls
                samples.append(gpu_used())
                time.sleep(0.4)

        th = threading.Thread(target=poll, daemon=True)
        th.start()
        try:
            r = subprocess.run([PYEMBED, "-s", BENCH, "cap_%s_%d" % (precision, workers), TEXT,
                                "space_marine"], capture_output=True, text=True, env=env)
        finally:
            stop.set()
            th.join(timeout=4)
        peak_gpu = max(samples)
        died = "WORKER DIED" in (r.stdout + r.stderr) or "worker died" in (r.stdout + r.stderr)
        m_sum = re.search(r"(\d+) items, ([\d.]+)s spoken", r.stdout)
        items = int(m_sum.group(1)) if m_sum else 0        # the bench prints ONE summary line;
        spoken = float(m_sum.group(2)) if m_sum else 0.0   # counting "ok" lines read 1 every time
        wall = re.search(r"wall=\s*([\d.]+)s", r.stdout)
        live_priv = {pid: priv_gb(pid) for pid, _ in _procs()}
        hold_gb = max([v for k, v in idle_priv.items() if dict(procs)[k] == "H"] or [-1])
        w_priv = [v for k, v in idle_priv.items() if dict(procs)[k] == "W" and v > 0]
        w_live = [v for k, v in live_priv.items() if v > 1.0]
        decoders = min(items, workers) or 1
        row = ("%-5s %d | idle gpu %5d -> peak %5d MiB (%+5d) | per-worker gpu %.2f GB over %d "
               "decoders | idle priv: holder %.1f, worker %.1f GB | mid-job priv max %.1f GB | "
               "%d items, %.1fs spoken, wall %ss") % (
            precision, workers, idle_gpu, peak_gpu, peak_gpu - idle_gpu,
            (peak_gpu - idle_gpu) / 1024.0 / decoders, decoders, hold_gb,
            max(w_priv) if w_priv else -1, max(w_live) if w_live else -1, items, spoken,
            wall.group(1) if wall else "?")
        print(row, flush=True)
        with open(OUT, "a", encoding="utf8") as fh:
            fh.write(row + "\n")


if __name__ == "__main__":
    main()
