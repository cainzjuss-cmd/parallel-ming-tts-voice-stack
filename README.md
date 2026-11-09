# Shared-weight Ming TTS pool - portable stack snapshot

Built from a working setup on Windows 10 / Ryzen 5950X / 64 GB / RTX 4090 24 GB, pinned
2026-11-08. This tree is the source + manifest + installers. It contains **no weights and no game
audio** - see `download_manifest.json` for where to get the model, and the "Voices" section below
for making your own reference clips.

## What it is

A voice-reply pipeline: Hermes answers in chat, the reply is spoken by a zero-shot cloned TTS
(Ming 3.0 TTS 0.5B) inside ComfyUI, in a chosen character voice.

The part worth having is the **shared CUDA-IPC weight pool**:

- one holder process loads the model **once** (`files/ming_weightd.py`);
- it spawns N ComfyUI workers in-process (`files/ming_worker.py` over `torch.multiprocessing`);
- each worker maps the *same* GPU tensors and **skips its own checkpoint load** (a hook in the
  node's `loader.py`), so the weights are not duplicated N times;
- the spooler (`files/dow_spool.py`) splits a reply into sentences, sends each sentence to a
  free worker, and plays the results back in strict order with a single player;
- `files/tts_pool.py` holds the settings (precision, worker count, flavour text) and a capacity
  guard that decides how many workers actually fit; `files/cap_probe.py` produced the numbers.

Measured, same 6-sentence / 40.7 s reply:

| mode | wall | first audio | GPU idle | GPU peak |
|---|---:|---:|---:|---:|
| 3 workers, shared weights | 40.1 s | +15.2 s | 16.4 GB | 16.9 GB |
| 2 workers, independent streaming | 56.4 s | +17.6 s | 12.1 GB | 17.6 GB |

Marginal cost of an extra worker is small (0.07-0.11 GB VRAM per concurrent decode) because the
weights are shared; the thing that limits worker count on Windows is **private commit** against
the pagefile, not VRAM. Per-process commit of a warmed worker measured 4.1-4.5 GB by reading
that process directly.

Worker count does **not** make one reply faster at the quality settings below (wall was 48-53 s
at 1, 2, 3, 4, 5 and 6 fp32 workers). It buys concurrency between separate replies.

## Status of the Linux port - read before using it

**Rewired: yes. Running on Linux: never.** The tools now go through one cross-platform seam
(`files/stack_paths.py`: paths, spawn flags, memory/commit reads, process scans, private-commit
reads and the audio playback backend), and the rewiring is proven not to have changed what the
voice sounds like: `regression_bit_exact.sh` boots the PACK copy in a throwaway home, renders a
fixed control sentence, and compares BYTES with a control captured from the live stack before any
porting. It has been run twice (2026-11-08 22:48 and 2026-11-09 02:40), both
`RESULT: BIT-EXACT` — farseer, fp32, flow_steps 8, cap 160, flavour on: **9.45 s, 834,006 bytes,
md5 60f1f0b9f4f68a2a9e0217e3fb9b8e09**, 3 chunks, on a 3-worker pool with zero-copy bind evidence
for that boot's own worker pids.

What that gate does NOT prove: any line of this stack executing on Linux. The `selfcheck/` harness
imports every module with `os.name`/`sys.platform` faked to POSIX (which catches Windows-only calls
at import time and nothing else), and the POSIX branches for detached spawn, playback + barge-in,
`/proc` scanning and VmRSS/Private reads are written but unexecuted. `install_linux.sh` is the
honest path to a first boot, not a verified install. Re-run `cap_probe.py` on the target box: the
shipped `COEF` was fitted on Windows (pagefile-backed commit accounting, Windows allocator
behaviour) and is not portable.

## Install (Windows, today)

```
python dl.py                                   # fetches + verifies the checkpoint
python files/apply_share_hook.py --node <ComfyUI>/custom_nodes/ComfyUI-Ming-3-tts
copy files\*.py <hermes home>\tools\
set HERMES_HOME=%LOCALAPPDATA%\hermes
python files\dow_tts.py pool up                 # holder + workers, warmed
python files\dow_spool.py run --voice <id> --b64 <base64 text>
```

The desktop chip (`files/plugin.js`) goes in `<hermes home>\desktop-plugins\dow-voice-chime\` and
needs ⌘K → **Reload desktop plugins**; the file watcher is unreliable, so a plugin that "did
nothing" usually just needs that reload.

The applier deliberately refuses to patch anything it cannot match exactly - a wrong insertion in
a model loader produces plausible-but-wrong audio with no exception, which is the worst possible
failure mode. If it refuses, read the message and apply the hook by hand
(`docs/upstream-proposal.md` explains the placement and why skipping the load is safe).

## Voices (not shipped, on purpose)

The reference clips, their transcripts and the character lines in `voice.json` are audio/text
from licensed games and are not in this tree. To use it, make your own: a voice is a directory
under `tts-voices/<id>/` containing

- `ref.wav` - one **continuous** clip of the target voice (see the finding below),
- `ref_text.txt` - a **verbatim** transcript of that same audio (the clone conditions on it; a
  wrong transcript conditions wrong speech),
- `voice.json` - `{name, tag, language, catch, style, swaps, atempo, ...}`.

Finding worth repeating: reference **continuity beat reference length**. A single uninterrupted
14.7 s clip produced 6.78 s of audio for a 13-word sentence, while spliced references of 3.4,
7.5, 11.1 and 29.4 s produced 10.1-11.3 s for the same text. Generation time barely changed with
reference length (9.5-12.5 s). n=5, one voice, one sentence - a strong lead, not a law.

`files/build_dow_voices.py` shows the shape of an extraction+ASR pipeline against your own game
files; run it on audio you own, and note `atempo` calibration (chars/s of the reference vs the
synthesis, damped 0.55, clamped 0.75-1.30).

## Quality settings that matter

- **One sentence per chunk.** Long generations decay: past roughly 6 s of speech per decode the
  endings go digitally smeared. The chunker caps at 160 chars, splits at the nearest clause
  boundary, protects decimals and abbreviations, and merges fragments under 25 chars.
- `flow_steps 16` (env `DOW_FLOW_STEPS`). 50-100 was not worth it by ear on a Bluetooth speaker;
  it is on the headphones. `cfg 2.0`, `temperature 0`, `max_steps 250` - `cfg` and `max_steps`
  were dead ends for speed.
- Precision: `bf16` (fast, small) vs `fp32` (audibly smoother on editing headphones). Both run on
  the shared pool; `MING_FP32_STREAM=1` is the escape hatch back to streaming workers.
- Edge silence is trimmed per clip (`DOW_EDGE_FLOOR=-45dB`, `DOW_EDGES_OFF=1` to disable),
  otherwise each clip's own dead air turns a 280 ms gap into ~0.9 s.
- Barge-in: typing a new message stops speech mid-word (`tts-voices/.generation` token polled
  between and during items).

## The traps, so you don't repeat them

Full write-up in `docs/upstream-proposal.md`. Short version: share runtime tensors and never a
`state_dict` (wrong audio, no error); `QuantizedTensor` needs flatten/unflatten with inner args
first; the meta skeleton needs the quant slots *created*; `multiprocessing.spawn` re-imports the
launcher on Windows and steals `--port` unless argv is reset; plain `multiprocessing` cannot send
CUDA tensors. And the measurement trap: do not derive per-worker commit by differencing the
machine's total free commit - another large process (an LLM engine, here 19-26 GB) moves in the
same window and the arithmetic attributes its growth to your pool.

Three more, all met while running this gate:

- **An instrument that reports failure on success is worse than none.** The health probe tested
  curl's exit code, so it failed CLOSED: it declared a healthy pool dead. Judge the HTTP status
  (`-w '%{http_code}'` against 200) with a patient timeout and retries. Same class: the bind-path
  check looked for a share log at the path the shim *would* use, while the node actually installed
  on that box opens a hardcoded one, so a boot that had bound zero-copy printed "no share log".
  Correlate the evidence to the pids that boot created instead of to a file's existence.
- **Pool commands were not in the audit trail, so "the pool died" and "someone switched it off"
  were indistinguishable** - the plan recorded an unattributable loss because of it. Every
  state-changing `pool up|down|restart` now writes a line with the caller's pid (a chip click
  arrives from the gateway process, a hand/scripted boot does not). `status` is deliberately not
  logged; the UI polls it and the trail would fill with polls.
- **A hook that is installed by hand and a hook installed by the applier are different files.**
  Check which one the running box actually has before claiming the scripted one is what runs:
  this box's `loader.py` was hand-patched, honours `MING_KEEP_LOAD`, and writes a hardcoded log
  path, and there is no `loader.py.orig` to revert to (rollback = `node_patch/` in the snapshot).
  `apply_share_hook.py` REFUSES to overwrite an existing hook, which is correct but leaves the
  divergence invisible until a render behaves differently from the pack's documentation.

Also, when a pipeline prints "N items, X.Xs spoken", that number is the sum of the generated clips
and excludes the gap clips the concat adds - it reads ~0.5 s short of the file's own duration on a
3-chunk reply. Judge a render by the FILE, never by the printed line.

## Files

```
files/dow_tts.py         pool lifecycle + settings CLI (up|down|status|restart, settings, setting)
files/dow_spool.py       reply -> sentences -> workers -> ordered playback, barge-in
files/tts_pool.py        settings, live-pool announcement, capacity guard, COEF
files/ming_weightd.py    holder: loads once, spawns workers, warm-up, boot memory trace
files/ming_worker.py     pack/unpack over CUDA IPC, zero-copy bind into a meta model
files/cap_probe.py       per-worker VRAM/commit measurement sweep -> cap_measure.txt
files/mem_trim.py        Windows-only bounded working-set release (DOW_TRIM_NEEDED_MB)
files/comfy_workers.ps1  Windows launcher for the pool
files/stack_paths.py     cross-platform shim: paths, spawn flags, meminfo, procs, playback
files/apply_share_hook.py idempotent loader.py hook installer/refuser
files/plugin.js          desktop chip: voice, precision, workers 1-7 (red = does not fit), flavour
docs/upstream-proposal.md the upstream-facing write-up (draft, not filed anywhere)
docs/PORT_STATUS.md      what the port changed, what it verified, what it could not
download_manifest.json + dl.py  pinned sources, sizes, sha256; resumable verified fetching
regression_bit_exact.sh  the gate: live pool down -> pack pool in a sandbox home -> render -> bytes
                         LEAVE_DOWN=1 keeps the pool off afterwards (honour the user's standing
                         state instead of restoring a service they turned off)
selfcheck/               pathprobe.py (resolves to the sandbox or aborts), headroom_read.py,
                         compare_control.py (byte verdict), check_fake_posix.py
regress_run1_BITEXACT.log / control_pack_render_run1.wav   the first PASS, kept as evidence
```

Licence: **MIT**, chosen by the owner on 2026-11-08. Intended repo name on publication:
`parallel-ming-tts-voice-stack`. Nothing is published yet, and the exclusion rule below has to be
applied first: game audio, extracted speech banks, `ref.wav`s, ASR transcripts of game lines,
voice.json catch/persona text and `node_patch/loader.py` (a copy of a third-party file) stay out.
