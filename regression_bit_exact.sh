#!/usr/bin/env bash
# Bit-exact regression for the Windows->Linux rewiring of the pack copy.
#
# Why this exists: the port is a refactor of a working system, and "it imports fine on Linux"
# proves nothing about the thing that matters - that the voice still sounds EXACTLY the same on
# Windows. The only honest gate is a render whose bytes match a pre-port control, so that is what
# this checks. If the md5 changes, the refactor changed behaviour and must be fixed, not accepted.
#
# Control (captured 2026-11-08 from the LIVE stack before any porting):
#   text   : "The control exists so that a later refactor cannot quietly change what the voice
#             sounds like."
#   voice  : farseer   precision: fp32   flow_steps: 8   max_chunk: 160   flavour: on
#   result : 9.45 s, 834,006 bytes, 3 chunks, md5 60f1f0b9f4f68a2a9e0217e3fb9b8e09
#
# Disruptive by design: it stops the live pool, boots the PACK pool on the same ports for a few
# minutes, then puts the live pool back. The trap makes the restore happen even on Ctrl-C or a
# kill, because a half-run that leaves the machine with no pool is worse than a failed test.
set -uo pipefail

# NATIVE forward-slash paths throughout. git-bash accepts them for its own file ops, and they are
# what Windows python needs as argv. The MSYS /c/... form is what bit this script twice: it gets
# handed to python.exe as C:\c\Users\... and the tool never runs, which then looks like a failed
# port instead of a failed harness.
PACK="C:/Users/Sander/tts-stack-pack"
SB="$PACK/testhome"
LIVEH="C:/Users/Sander/AppData/Local/hermes"
LIVECOMFY="$LIVEH/tools"
COMFY_NATIVE="O:/ComfyUI_windows_portable_nvidia/ComfyUI_windows_portable"
VP="C:/Users/Sander/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe"
PYEMB="$COMFY_NATIVE/python_embeded/python.exe"
WANT="60f1f0b9f4f68a2a9e0217e3fb9b8e09"
CTRL='The control exists so that a later refactor cannot quietly change what the voice sounds like.'

# Health probe with retries and a patient timeout. A bare 2 s curl against a busy worker is how a
# live pool gets misread as dead - it happened for real earlier today and produced a false alarm,
# so the probe has to be boring rather than clever: three tries, 6 s each, on /system_stats.
# It checks the HTTP status, not curl's exit code: an exit-code probe fails CLOSED, and a probe
# that reports "dead" while the pool is healthy is worse than no probe at all.
up() {
    for _ in 1 2 3; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://127.0.0.1:$1/system_stats")
        [ "$code" = "200" ] && return 0
        sleep 2
    done
    return 1
}
answ()   { local s=""; for p in "$@"; do up "$p" && s="$s$p "; done; printf '%s' "$s"; }
portclear() { for _ in $(seq 1 40); do local busy=0; for p in 8188 8189 8190; do up "$p" && busy=1; done; [ "$busy" = 0 ] && return 0; sleep 1; done; return 1; }

restore_live() {
    echo "[regress] restoring the LIVE pool ... (LEAVE_DOWN=${LEAVE_DOWN:-0})"
    HERMES_HOME="C:/Users/Sander/AppData/Local/hermes" COMFY_ROOT="$COMFY_NATIVE" \
        "$VP" -s "$LIVEH/tools/dow_tts.py" pool down >/dev/null 2>&1 || true
    # The pool was running for this test only, at the user's request; their standing choice is
    # that it is off afterwards, so restoring it against that intent would be wrong. The trap
    # still tears the PACK pool down, which is what protects the machine if the run is killed.
    if [ "${LEAVE_DOWN:-0}" = "1" ]; then
        echo "[regress] live pool left DOWN (user's standing choice)"
        return
    fi
    HERMES_HOME="C:/Users/Sander/AppData/Local/hermes" COMFY_ROOT="$COMFY_NATIVE" \
        "$VP" -s "$LIVEH/tools/dow_tts.py" pool up   >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do up 8188 && break; sleep 3; done
    echo "[regress] live pool answering: $(answ 8188 8189 8190)"
}
trap restore_live EXIT TERM INT

echo "== 1. sandbox home (env must override files/tts_pack.json, which pins this box's live roots)"
rm -rf "$SB"; mkdir -p "$SB/tools" "$SB/tts-voices" "$SB/cache/scratch" "$SB/tts-ab"
cp "$PACK/files/"*.py "$SB/tools/"
cat > "$SB/tools/tts_pack.json" <<JSON
{
  "hermes_home": "C:/Users/Sander/tts-stack-pack/testhome",
  "comfy_root":  "O:/ComfyUI_windows_portable_nvidia/ComfyUI_windows_portable",
  "model_dir":   "E:/ai-models/comfy/ming-tts",
  "share_log":   "C:/Users/Sander/tts-stack-pack/testhome/cache/scratch/share_path.log"
}
JSON
cp -r "$LIVEH/tts-voices/farseer" "$SB/tts-voices/"
printf '{\n "precision": "fp32",\n "workers": 3,\n "flavor": true\n}\n' > "$SB/tts-voices/pool.json"
echo "   sandbox: $SB  (its own pool.json, voices and scratch)"

# Step 2 runs as a FILE, not `python -c "..."`: handing an inline blob to a native .exe is
# what produced the fake failures earlier (an MSYS path inside the blob became C:\c\...).
# It must ABORT the run here, before anything is torn down: a pack copy that resolves to
# the live home would drive the real deployment and the test would prove nothing.
echo "== 2. confirm the pack resolves to the SANDBOX, not to the live home"
HERMES_HOME="$SB" COMFY_ROOT="$COMFY_NATIVE" MING_SHARE_LOG="$SB/cache/scratch/share_path.log" \
  "$VP" "$PACK/selfcheck/pathprobe.py"
[ $? -ne 0 ] && { echo "   ABORT: the pack does not resolve to the sandbox. Nothing was stopped."; exit 1; }

echo "== 3. stop the LIVE pool (replies go silent from here until the restore)"
HERMES_HOME="C:/Users/Sander/AppData/Local/hermes" COMFY_ROOT="$COMFY_NATIVE" \
  "$VP" -s "$LIVEH/tools/dow_tts.py" pool down
if ! portclear; then
    echo "   ABORT: ports still answering after pool down. Refusing to boot a second pool beside a"
    echo "   live one - two weight sets on one card would blow VRAM and make this test's numbers"
    echo "   meaningless. The trap will put the live pool back."
    exit 1
fi
echo "   ports clear"
# Settle on resources, not on ports: CUDA IPC unmap and worker teardown keep draining the card
# for seconds after the last port stops answering. Booting into that window makes the capacity
# guard read a busy card and clamp the worker count, so the run tests a 1-worker pool while the
# standing config says 3. Poll whole-GPU usage until it stops falling (3 samples within 50 MiB).
prev=999999; stable=0; polls=0
for _ in $(seq 1 40); do
    polls=$((polls+1))
    now=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' \r')
    if [ "$(( now > prev ? now - prev : prev - now ))" -le 50 ]; then stable=$((stable+1)); else stable=0; fi
    prev=$now
    [ "$stable" -ge 3 ] && break
    sleep 1
done
echo "   card settled at ${prev} MiB after $polls polls; now reading headroom through the pack shim"
"$VP" "$PACK/selfcheck/headroom_read.py" "$SB/tools"

echo "== 4. boot the PACK pool"
OFF_LOG=0
HERMES_HOME="$SB" COMFY_ROOT="$COMFY_NATIVE" MING_SHARE_LOG="$SB/cache/scratch/share_path.log" \
  "$VP" -s "$SB/tools/dow_tts.py" pool up
WLOG="$SB/cache/scratch/weightd.log"; touch "$WLOG"; OFF_LOG=$(stat -c%s "$WLOG")
for _ in $(seq 1 200); do tail -c +$((OFF_LOG+1)) "$WLOG" | grep -aq "pool ready" && break; sleep 1; done
tail -c +$((OFF_LOG+1)) "$WLOG" | grep -a "pool ready\|worker died\|warm " | tail -3 | cut -c1-100
echo "   answering: $(answ 8188 8189 8190)"
echo "   bind path used (ZERO-COPY expected, not KEEP-LOAD):"
# Judged BY THE PIDS THIS BOOT CREATED, not by "did some file appear". The node installed on
# this box predates apply_share_hook.py's env-driven log path - it opens a hardcoded file - so
# the sandbox's own share_path.log legitimately stays empty here. The old check printed
# "(no share log written)" on a boot that had in fact bound zero-copy: an instrument reporting
# failure on success, which is the third one of those today.
WLOG="$SB/cache/scratch/weightd.log"
WP=$(tail -c +$((OFF_LOG+1)) "$WLOG" | grep -a "spawned .* pid " | grep -o "pid [0-9]*" | cut -d' ' -f2 | tr '\n' ' ')
echo "     worker pids this boot: ${WP:-<none logged>}"
found=0
for lg in "$SB/cache/scratch/share_path.log" "$LIVEH/cache/scratch/share_path.log"; do
    [ -f "$lg" ] || continue
    for pid in $WP; do
        line=$(grep -a "^$pid " "$lg" | tail -1 | cut -c1-60)
        if [ -n "$line" ]; then found=1; echo "     pid $pid -> $line   [log: $lg]"; fi
    done
done
[ "$found" = "1" ] || echo "     NO bind evidence for this boot's pids - treat the render's speed numbers as unknown, the md5 verdict still stands"

echo "== 5. render the control through the PACK spooler"
B=$(printf '%s' "$CTRL" | base64 -w0)
rm -f "$SB"/cache/scratch/spool_farseer_*.wav
# Keep the RAW tail, not a grep: three earlier runs printed a summary line that hid the actual
# error. Note the printed "N items, X.Xs spoken" is the SUM OF THE ITEM CLIPS only - the 280 ms
# gap clips that the concat adds are not counted, so it reads ~0.5s short of the file's own
# duration. Compare the FILE (compare_control.py), never that number, against 9.45 s.
HERMES_HOME="$SB" COMFY_ROOT="$COMFY_NATIVE" DOW_NOPLAY=1 DOW_FLOW_STEPS=8 DOW_MAX_CHUNK=160 \
  timeout 240 "$PYEMB" -s "$SB/tools/dow_spool.py" run --voice farseer --b64 "$B" 2>&1 | tr -d '\r' | tail -6
F=$(ls -t "$SB"/cache/scratch/spool_farseer_*.wav 2>/dev/null | head -1)
[ -z "$F" ] && { echo "   NO OUTPUT FROM THE PACK SPOOLER - regression FAILED (check steps 2-4 first: this has been a harness bug every time)"; exit 1; }
cp "$F" "$PACK/control_pack_render.wav"
"$VP" "$PACK/selfcheck/compare_control.py" "$PACK/control_pack_render.wav" "$WANT"
RC=$?

echo "== 6. one AUDIBLE run through the pack (the benchmark path skips playback, so the"
echo "      shim's PlaySound/barge-in code is otherwise untested)"
B2=$(printf '%s' "Flavour and playback both have to work." | base64 -w0)
HERMES_HOME="$SB" COMFY_ROOT="$COMFY_NATIVE" DOW_FLOW_STEPS=8 DOW_MAX_CHUNK=160 \
  timeout 180 "$PYEMB" -s "$SB/tools/dow_spool.py" run --voice farseer --b64 "$B2" 2>&1 | tr -d '\r' | tail -4

echo "== 7. pack pool down (live restore runs from the trap on exit)"
HERMES_HOME="$SB" COMFY_ROOT="$COMFY_NATIVE" "$VP" -s "$SB/tools/dow_tts.py" pool down
echo "== regression render exit code: $RC"
exit $RC
