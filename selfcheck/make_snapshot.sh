#!/usr/bin/env bash
# Refresh the private rollback snapshot from the LIVE tree (never from a previous staging copy -
# a staged tree silently keeps reverted constants and drops features added after staging).
set -uo pipefail
LIVEH="C:/Users/Sander/AppData/Local/hermes"
NODE="O:/ComfyUI_windows_portable_nvidia/ComfyUI_windows_portable/ComfyUI/custom_nodes/ComfyUI-Ming-3-tts"
OUT="C:/Users/Sander/tts-stack-pack/WINDOWS_SNAPSHOT_2026-11-09"
TOOLS="build_dow_voices.py cap_probe.py comfy_workers.ps1 dow_spool.py dow_tts.py mem_trim.py ming_spike.py ming_weightd.py ming_worker.py tts_pool.py"

rm -rf "$OUT"; mkdir -p "$OUT/tools" "$OUT/chip" "$OUT/settings" "$OUT/node_patch"

for f in $TOOLS; do cp "$LIVEH/tools/$f" "$OUT/tools/" || echo "MISSING TOOL $f"; done
cp "$LIVEH/desktop-plugins/dow-voice-chime/plugin.js" "$OUT/chip/plugin.js"
cp "$LIVEH/tts-voices/pool.json" "$OUT/settings/pool.json"
for v in bonesinger farseer flash_gitz librarian slugga_boy space_marine; do
    cp "$LIVEH/tts-voices/$v/voice.json" "$OUT/settings/voice_$v.json" || echo "MISSING VOICE $v"
done
cp "$NODE/loader.py" "$OUT/node_patch/loader.py.as-patched-2026-11-09"

{
  echo "Rollback snapshot taken 2026-11-09 02:47 from the LIVE tree (sha256 of every file in SHA256SUMS.txt)."
  echo "Contents: the 10 TTS tools, the desktop chip, pool.json + the six voice.json files (atempo/catch/swaps"
  echo "only - NO audio, NO ref_text.txt, NO ASR transcripts), and the node's loader.py exactly as patched."
  echo
  echo "This box's node hook was written BY HAND, not by files/apply_share_hook.py. It honours"
  echo "MING_KEEP_LOAD and writes its share log to a hardcoded path (cache/scratch/share_path.log)."
  echo "There is NO loader.py.orig on this box, so apply_share_hook.py --revert CANNOT restore upstream"
  echo "here. Correct node rollback = copy this file back over the node's loader.py, then restart the pool:"
  echo "  cp node_patch/loader.py.as-patched-2026-11-09 <node>/loader.py"
  echo "  python dow_tts.py pool down && python dow_tts.py pool up"
  echo
  echo "Tools/chip rollback = copy tools/*.py and chip/plugin.js back, then Reload desktop plugins."
  echo "State at snapshot time: pool OFF (the user's choice at 02:40 via the chip's off switch and the"
  echo "regression's LEAVE_DOWN=1), pool.json says precision=fp32 workers=3 flavor=true."
  echo
  echo "NEW in this snapshot vs 2026-11-08: dow_tts.py writes one audit line per state-changing pool"
  echo "command (cache/scratch/pool_settings.log: 'POOL ppid=<caller> cmd=up|down|restart'), so an"
  echo "off-click at the chip is now distinguishable from a spontaneous death. Status polls are not"
  echo "logged (the chip polls every 45 s)."
  echo
  echo "PRIVATE: node_patch/ and settings/voice_*.json carry third-party code and licensed game text."
  echo "Strip both directories before any public push."
} > "$OUT/README-SNAPSHOT.txt"

( cd "$OUT" && find . -type f ! -name SHA256SUMS.txt ! -name FILES.txt | sed 's|^\./||' | sort > FILES.txt && sha256sum $(cat FILES.txt) > SHA256SUMS.txt )
echo "=== manifest (first 12) ==="; head -12 "$OUT/SHA256SUMS.txt"
echo "=== audio check inside the snapshot (must be empty) ==="
find "$OUT" -iname "*.wav" -o -iname "*.flac" -o -iname "*.mp3" -o -iname "*.ogg" -o -iname "ref_text.txt" -o -iname "*transcript*"
echo "=== zip it ==="
cd /c/Users/Sander/tts-stack-pack && rm -f WINDOWS_SNAPSHOT_2026-11-09.zip && powershell.exe -NoProfile -Command "Compress-Archive -Path 'C:/Users/Sander/tts-stack-pack/WINDOWS_SNAPSHOT_2026-11-09' -DestinationPath 'C:/Users/Sander/tts-stack-pack/WINDOWS_SNAPSHOT_2026-11-09.zip' -Force" && ls -la WINDOWS_SNAPSHOT_2026-11-09.zip && sha256sum WINDOWS_SNAPSHOT_2026-11-09.zip
