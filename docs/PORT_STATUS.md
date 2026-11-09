# PORT_STATUS.md — Windows→Linux rewiring of files/ onto stack_paths.py

Date: 2026-11-08. Scope: `files/` tree only. No live files under
`C:/Users/Sander/AppData/Local/hermes/tools/` were touched. No pool process was
started/stopped/restarted, no render run (bit-exact regression is the parent's).
`files/plugin.js` untouched.

## Critical finding first — source-of-truth sync

`files/tts_pool.py` (copied 04:14) had DIVERGED from the live tools
(`WINDOWS_SNAPSHOT_2026-11-08/tools/`, live mtime 10:06 — the newest):

- live `COMMIT_MARGIN_GB = 6.0`; the files/ copy carried `8.0` with the
  "8, not 6" comment — this matches the parent's "previous attempt ... was wrong
  and was reverted by the user" description. The reverted (6.0) value and its
  worker_commit-differencing note were restored, per instruction "COMMIT_MARGIN_GB=6.0".
- live has the `flavor` feature (DEFAULTS key, `set_flavor()`, dow_tts
  `--flavor`, dow_spool stylize gate); live `tts-voices/pool.json` actually
  contains `"flavor": true`. The stale files/ copy would have DROPPED that key on
  every `setting` write (settings() filters by DEFAULTS). The live hunks were
  ported verbatim (flavor gates catch+style only; swaps/atempo unaffected;
  flavor-only change does not restart the pool).

All capacity math otherwise untouched: COEF {0.10/0.20 worker_gpu, 4.5
worker_commit, 3.5/4.0 holder_commit}, GPU_MARGIN_GB=1.5, COMMIT_MARGIN_GB=6.0,
MAX_WORKERS=7, no hard ceiling, reclaim_live, STATIC_FLOOR. A comment block above
COEF now marks them WINDOWS-MEASURED and says to re-measure via cap_probe.py on
Linux. Verified: pack `settings` output == live `settings` output modulo live
sampling jitter (same max=4, same coef, same arithmetic line).

## stack_paths.py (two edits; otherwise untouched)

- FIX: `play_wav` called `winsound.PlayFile` — does not exist. Replaced with
  `winsound.PlaySound(path, SND_FILENAME | SND_ASYNC?)`, byte-equivalent to the
  inline calls it replaces (verified against the old dow_tts/dow_spool call sites).
- ADDED public `env(name, cfg_key, default)` (thin wrapper over `_env`) for
  config keys with no primitive: audiocpp_bin/audiocpp_model, voice_src, venv_python.
  Resolution order identical (env > tts_pack.json > default).

## New file: files/tts_pack.json

Pins THIS box's roots so the pack copy resolves identically to the live
deployment: hermes_home (C:), comfy_root (O:\ComfyUI_windows_portable_nvidia\...),
audiocpp_bin, audiocpp_model, voice_src. Verified: sp.{HERMES_HOME,TOOLS_DIR,SCRATCH,
VOICES,COMFY_ROOT,MODEL_DIR,SHARE_LOG} all match the old hardcoded values
(casing/separator-insensitive). Fresh installs get their own from
install_windows.ps1 / install_linux.sh; env vars override this file.
NOTE: installers do NOT write audiocpp_* / voice_src keys — see Gaps.

## Per-file call replacements

### tts_pool.py
- HERMES hardcode → sp.HERMES_HOME (attribute kept; no remaining consumers).
- SETTINGS = sp.VOICES/pool.json; ACTUAL = sp.SCRATCH/ming_weightd.actual (same
  filenames). Sibling-import insert so `import stack_paths` works under python_embeded ._pth.
- `_avail_gb()` GlobalMemoryStatusEx inline → sp.mem_avail(); `_gpu_gb()` inline
  nvidia-smi → sp.gpu_mem(). Function names kept (dow_tts settle loop uses `_gpu_gb`).
- + flavor (from live). COEF math untouched.

### dow_tts.py
- Module-level `winsound` import removed.
- HERMES/COMFY_IN/COMFY_OUT/TMP → sp; VOICES keeps HERMES_VOICES env first, else sp.VOICES.
- AUDIOCPP/AUDIOCPP_MODEL → sp.env(...) default None; synth_audiocpp raises a clear
  "not configured" if missing (this box: pinned in tts_pack.json → identical path used).
- PORTABLE=sp.COMFY_ROOT; PYEMBED=sp.python_in_comfy_root(); PIDFILE=sp.SCRATCH/... same name.
- HOLDER script-relative (_TOOLS/ming_weightd.py): identical value on the deployed box
  (tools dir = HERMES/tools), pack-self-consistent otherwise (see Risks #3).
- pid_alive: tasklist under sp.OS=="windows"; POSIX os.kill(pid,0).
- holder_pid fallback: inline Get-CimInstance ps → sp.processes("ming_weightd").
- pool_down: Windows branch (taskkill /T /F + the exact CIM/Stop-Process sweep) kept
  verbatim under sp.OS; POSIX branch sweeps sp.processes("ming_weightd|multiprocessing\.spawn
  |multiprocessing\.popen|ComfyUI/main\.py.*--port (ports)\b") + SIGKILL. Same target set
  modulo the ExecutablePath pin (no /proc equivalent; port regex bounds it instead).
- All 5 detached spawns (streaming workers, holder, restart child, play child, and the
  spool's run child in dow_spool) → **sp.SPAWN_DETACH. None had STARTUPINFO.
- stop: taskkill /IM under sp.OS; POSIX SIGTERMs other dow_tts.py procs (excluding self).
- play: sp.play_wav(interruptible=False); POSIX waits the player child and owns it via
  SIGTERM handler + finally(stop_play). Windows: same blocking PlaySound(SND_FILENAME).
- + --flavor / set_flavor wiring (from live, incl. the no-restart flavor-only path).

### dow_spool.py
- `winsound` import removed; HERMES/VOICES/TMP/TOKEN → sp (`.generation` token path and
  filename unchanged); COMFY_IN/OUT → sp.COMFY_ROOT/ComfyUI/*.
- start → **sp.SPAWN_DETACH.
- Playlist: identical loop structure, gap (280 ms), in-order gate (wait on slot i-1,
  never skip/overlap), 0.1/0.25 s token polling between AND mid-item, poll window
  `t_p + max(0.05, d - 0.12)`, chunker (HARD_CHUNK 160, protect/fold rules), atempo,
  edge trim (-45 dB), flow_steps 16, seeds untouched. Each of the 4 inline
  `PlaySound(None, SND_PURGE)` / 1 `PlaySound(path, SND_FILENAME|SND_ASYNC)` became
  sp.stop_play(player)/sp.play_wav(path.replace("\\","/"), interruptible=True).
  Single owner: the `player` local in the playlist loop; POSIX stop_play = SIGTERM to
  the previous/active child before the next play = PlaySound's implicit-stop parity
  (the previous item's unwaited 0.12 s tail is cut at the same instant on both OSes).
- + stylize flavor gate (from live).

### ming_weightd.py
- ROOT → sp.COMFY_ROOT/ComfyUI; sys.path insert before shim import (spawn re-imports
  this module; ._pth exclusion).
- gpu_used() → sp.gpu_mem() (MiB round-trip is exact: nvidia-smi answers whole MiB; -1 on failure kept).
- warm_reference voices dir → sp.VOICES; warm clip lands in ROOT/input (COMFY-anchored).
- _mem(): commit → sp.priv_gb(os.getpid()) (same psapi read on Windows); working set
  stays PMC/WorkingSetSize under sp.OS=="windows", POSIX VmRSS.
- empty_working_set(): sp.OS guard, POSIX returns False (caller only logs the delta).

### ming_worker.py
- ROOT → sp.COMFY_ROOT/ComfyUI; TOOLS insert before shim import so the spawn-reimport
  is side-effect-free on every OS. IPC/pack/unpack code untouched.

### cap_probe.py (rewritten, measurement math untouched)
- ctypes import + module-level PMC/priv_gb (psapi) → sp.priv_gb; inline CIM procs →
  sp.processes("ming_weightd|spawn_main") with same H/W classification; nvidia-smi → sp.gpu_mem.
- HERMES/SCRATCH/HOLDER_LOG/OUT/BENCH → sp roots (same filenames); VP = hermes-agent venv
  python computed per OS (equals old hardcode on this box; DOW_VENV_PY/venv_python override);
  PYEMBED → sp.python_in_comfy_root; TOOLS/DOW_TTS/TEXT script-relative.
- Docstring now says this tool must RE-MEASURE COEF on Linux before trusting the guard.

### mem_trim.py
- Windows machinery (WinDLL kernel32/psapi, PMC, pids/probe/trim) moved into
  `_trim_windows`, entered only under sp.OS=="windows"; body byte-identical, sleep(3)
  kept at the same point. POSIX: prints the reason, exit 0 (dow_spool calls it
  unconditionally per batch; a crash there would look like a stack fault). Import-safe.

### build_dow_voices.py
- Module-level execution (which crashed on import without O:) wrapped in main() + __main__ guard.
- SRC → sp.env("TTS_VOICE_SRC","voice_src") (None → clear message, licensed material not shipped;
  this box pins it in tts_pack.json); DST → sp.VOICES (same tts-voices dir as runtime).
- VOICES/ORK data and copy/transcript/provenance logic untouched.

### apply_share_hook.py — unchanged. No absolute paths, cross-platform already.

### comfy_workers.ps1 — unchanged (Windows manual launcher; POSIX equivalent is
`python dow_tts.py pool up` + install_linux.sh; no untested bash mirror was added).

## Verification performed (all on this Windows box)

- `py_compile` (venv 3.11.16) on all 10 .py in files/: PASS.
- Code-line drift audit vs WINDOWS_SNAPSHOT tools: only the documented shim hunks
  differ; flavor and capacity code lines are identical to live.
- Static sweep: no `creationflags`/`STARTUPINFO`/DETACHED literals in tools; all
  windll/WinDLL/winsound/CIM/taskkill/tasklist hits are inside stack_paths or
  sp.OS=="windows" branches; no `[A-Z]:\` literals in tools .py outside stack_paths defaults.
- selfcheck/check_fake_posix.py: fresh subprocess per module, winsound poisoned
  (import → error), ctypes.windll=None, WinDLL/WINFUNCTYPE raise, os.name/sys.platform
  faked before shim import, all roots pinned to selfcheck/fakehome: ALL 10 modules
  import clean, shim resolves linux, SPAWN_DETACH=={start_new_session}; mem_trim RUN
  (as __main__) prints the no-op with rc=0. (Stdlib prewarm added — fake os.name
  must not break CPython's own lazy posix/shutil picks; documented in the harness.)
- Windows parity smoke (read-only): pack vs live `settings` (same coef/margins/max,
  numbers differ only by live sampling jitter) and `pool status` (`up 3/3, holder 28224`
  both — the shim's CIM process scan finds the same holder).
- Path-resolution assert: every sp root == the old hardcoded value.

## Not verified / cannot verify here

- Anything at RUNTIME on real Linux: detached spawn survival (start_new_session),
  ffplay/pw-play/paplay playback + SIGTERM barge-in mid-item, /proc cmdline scanning
  (pool_down/stop/cap_probe POSIX branches), VmRSS/Private_Dirty reads, python_in_comfy_root
  venv discovery, os.kill pid probes. No Linux host was available; fake-POSIX import
  checks only prove import-time cleanliness.
- Actual audio playback on Windows (would emit sound / a render): sp.play_wav Windows
  path is the same PlaySound call the inline code made, but was not exercised here.
- mem_trim against a live process (not run — it would mutate the engine's working-set;
  code is moved verbatim inside a function, compiled, and POSIX no-op smoke-tested).
- cap_probe end-to-end (it restarts the pool by design — forbidden for this task).
- audio.cpp cold fallback (`say` path) — the pool is up, so `say` uses Comfy; the
  fallback's config plumbing (tts_pack.json keys) was only unit-checked via sp.env.
- Pool JSON/HTTP behaviour with the shim's processes() on non-`python.exe` images
  (shim filters name='python.exe', same as the old inline CIM — unchanged assumption).

## Risks the parent's regression run should watch

1. `files/dow_tts.py pool up/restart` now spawns files/ming_weightd.py (script-relative),
   NOT the deployed HERMES/tools holder. Intended (test the ported tree), but mixing
   trees in one boot would surprise.
2. Holder/streaming spawns gained CREATE_NO_WINDOW (SPAWN_DETACH unification per task
   instruction). Should be invisible (stdio are files), but watch for spawn-time errors.
3. dow_spool now calls stop_play (SND_PURGE on Windows) immediately BEFORE each
   PlaySound — the previous item's tail is cut microseconds earlier than the implicit
   stop did before. Gap/playlist timing windows unchanged.
4. pool.json flavor key: ported code preserves it (old files/ copy silently deleted it
   on any `setting` write). If the regression compares against pre-port renders made
   while a stale files/ copy had stripped flavor, catch/style text could differ.
5. COMMIT_MARGIN_GB is now 6.0 (live truth). A regression that assumes the files/
   tree's former 8.0 will get different `capacity["max"]` values — that change is
   deliberate parity, not a re-tune.
6. POSIX mem_trim no longer sleeps 3 s before the batch (nothing to settle); Windows path keeps it.
7. files/tts_pack.json is this-box-pinned. On any other box delete/regenerate it (or set env);
   installers do not write audiocpp_*/voice_src keys — audio.cpp fallback and voice builder
   need them added there or via env DOW_AUDIOCPP/DOW_AUDIOCPP_MODEL/TTS_VOICE_SRC.
8. files/plugin.js is the older chip (no flavor toggle) but the .py side now matches live;
   snapshot chip (WINDOWS_SNAPSHOT_2026-11-08/chip/plugin.js) is the matching one if the
   flavor UI is exercised.
9. Em-dash in the flavor-only `setting` reply is written as `\u2014` source escape —
   runtime string identical to live's literal.
10. If the regression runs `dow_tts.py stop` while something plays: POSIX sweep SIGTERMs
    play owners (player dies with owner); Windows behavior untouched (taskkill /IM, which
    historically matched nothing — preserved as-is).

---

## Gate runs after the port (parent, 2026-11-08/09) — the verdict this file was waiting for

`regression_bit_exact.sh` renders a fixed control sentence through the PACK copy in a sandbox
home and compares BYTES to a control captured from the live stack before any porting.

| run | time | pool size | verdict | artifact |
|---|---|---|---|---|
| 0 (control, live tools) | 2026-11-08 21:03 | 3 fp32 | control reproduced: `60f1f0b9f4f68a2a9e0217e3fb9b8e09`, 834,006 B | `tts-ab/PORT_CONTROL_2026-11-08.wav` |
| 1 | 2026-11-08 22:48 | **1** worker (guard clamped: card still draining) | **BIT-EXACT** | `regress_run1_BITEXACT.log`, `control_pack_render_run1.wav` |
| 2 | 2026-11-09 02:40 | **3** workers, ZERO-COPY by pid | **BIT-EXACT** | `regress_run2.log`, `control_pack_render.wav` |

Control: farseer, fp32, `DOW_FLOW_STEPS=8`, `DOW_MAX_CHUNK=160`, flavour on, 9.45 s, 834,006 bytes,
md5 `60f1f0b9f4f68a2a9e0217e3fb9b8e09`, 3 chunks.

So the shim rewiring described above did not change a single output byte on Windows. The
`files/` tree is behaviourally equal to the deployed tools, and the sandbox resolves to its own
home (`pathprobe.py` aborts the run if it ever does not).

What the gate does NOT establish: any execution on Linux. Nothing here ran POSIX code for real —
detached spawn, ffplay/pw-play barge-in, `/proc` scans, VmRSS/Private reads, python discovery in a
Linux venv. `install_linux.sh` remains the honest path to a first boot.

### Fixes made while running the gate (harness + applier, not the port)

1. No `python -c` blobs against native exes; helpers live in `selfcheck/`.
2. Health probe judges `http_code == 200` (3 tries x 6 s), not curl's exit code.
3. Step 2 aborts BEFORE any teardown if the pack resolves to the live home.
4. After the live pool stops, wait for the card to settle (3 samples within 50 MiB) before
   booting; run 1 otherwise got a 1-worker pool and never exercised the standing configuration.
5. Bind evidence is correlated to the worker pids that boot logged, and searched in both the
   sandbox `SHARE_LOG` and the path the node installed on this box actually opens.
6. `restore_live()` honours `LEAVE_DOWN=1` — do not re-spawn a service the user switched off.
7. `apply_share_hook.py` BLOCK brought to parity with the hook this box actually runs: it now
   tests `MING_KEEP_LOAD` (previously documented but inert) and keeps the KEEP-LOAD post-load
   swap. Env-driven log path remains the only intentional difference.
8. `files/plugin.js` re-copied from live (the flavour toggle was missing from the pack copy).

### Divergence found on this box (do not "fix" it by re-running the applier)

The installed node `loader.py` was hand-patched (mtime 2026-11-07 19:30) and there is **no
`loader.py.orig`**, so `--revert` cannot restore upstream here — rollback is
`WINDOWS_SNAPSHOT_2026-11-09/node_patch/loader.py.as-patched-2026-11-09`. The applier refuses to
touch a file that already contains a MING_SHARE hook, which is the right behaviour and is why the
divergence had to be caught by reading the installed file.

### New in the live tools, synced into `files/`

`dow_tts.py` writes one audit line per state-changing pool command
(`cache/scratch/pool_settings.log`: `POOL ppid=<caller> cmd=up|down|restart`, plus the outcome of
`up`/`down`). Reason: a chip "off" click and a spontaneous death were indistinguishable, and the
plan's 05:10 entry recorded an unattributable loss as "cause unknown". Verified in run 2's trail.
Capacity math untouched in both trees (COEF, `GPU_MARGIN_GB=1.5`, `COMMIT_MARGIN_GB=6.0`,
`MAX_WORKERS=7`, `reclaim_live`, `STATIC_FLOOR`).
