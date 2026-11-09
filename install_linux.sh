#!/usr/bin/env bash
# Install the shared-weight Ming TTS stack on Ubuntu.
#   bash install_linux.sh [ComfyUI_root] [Hermes_home]
# Defaults: ~/ComfyUI and ~/.hermes (set HERMES_HOME if your Hermes stores its home elsewhere -
# the tools read that variable, they do not guess).
#
# READ THIS BEFORE EXPECTING IT TO WORK END TO END. The tools ARE rewired onto the cross-platform
# shim (files/stack_paths.py) and that rewiring is proven byte-exact on Windows: the pack copy
# renders a fixed control sentence identically to the live stack (regression_bit_exact.sh, two
# BIT-EXACT runs). What has never happened is a Linux RUN: the POSIX branches for detached spawn,
# playback/barge-in, /proc scanning and VmRSS/Private reads are written and fake-POSIX import-clean,
# but no Linux host has executed them. The capacity coefficients in tts_pool.py were fitted on
# Windows (pagefile-backed commit accounting, Windows allocator behaviour) and are NOT portable.
# Run cap_probe.py on this machine and use ITS numbers, not the shipped ones. Everything below is
# the honest path to a first boot, not a verified install.
set -euo pipefail

COMFY_ROOT="${1:-${COMFY_ROOT:-$HOME/ComfyUI}}"
HERMES_HOME="${2:-${HERMES_HOME:-$HOME/.hermes}}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# NODE is set once ComfyUI's location is known; setting it here with an unbound variable would
# abort the script under `set -u` before anything ran.

echo "== ComfyUI root : $COMFY_ROOT"
echo "== Hermes home  : $HERMES_HOME"

# --- system packages --------------------------------------------------------------
# ffmpeg: chunking/trim/probe. ffplay (or pw-play/paplay): playback + barge-in, because
# winsound does not exist on Linux and the spooler's player has to be terminable per chunk.
sudo apt-get update
sudo apt-get install -y git curl ffmpeg python3-venv python3-pip fonts-dejavu-core
if ! command -v ffplay >/dev/null 2>&1 && ! command -v pw-play >/dev/null 2>&1 && ! command -v paplay >/dev/null 2>&1; then
  echo "WARNING: no audio player found. Install ffmpeg (ffplay) or wireplumber (pw-play)." >&2
fi

# --- ComfyUI, pinned --------------------------------------------------------------
# v0.44.0 is what this stack was built against. Newer ComfyUI has changed memory management
# (and the quant ops the IPC transport depends on), so pin first, upgrade deliberately.
if [ ! -d "$COMFY_ROOT/ComfyUI" ]; then
  git clone https://github.com/comfyanonymous/ComfyUI "$COMFY_ROOT/ComfyUI"
fi
git -C "$COMFY_ROOT/ComfyUI" fetch --all --tags
git -C "$COMFY_ROOT/ComfyUI" checkout v0.44.0

python3 -m venv "$COMFY_ROOT/venv"
VP="$COMFY_ROOT/venv/bin/python"
"$VP" -m pip install -U pip wheel
# torch build must match your driver. Check `nvidia-smi` for the CUDA version it supports.
"$VP" -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
"$VP" -m pip install -r "$COMFY_ROOT/ComfyUI/requirements.txt"

# --- Ming node, pinned -----------------------------------------------------------
NODE="$COMFY_ROOT/ComfyUI/custom_nodes/ComfyUI-Ming-3-tts"
if [ ! -d "$NODE" ]; then
  git clone https://github.com/Saganaki22/ComfyUI-Ming-3-tts "$NODE"
fi
git -C "$NODE" fetch --all
git -C "$NODE" checkout ab3e358d4b15de8aad4bde90d149546e2d85485c   # 'Add Ming 3 TTS v1.0.0'
"$VP" -m pip install -r "$NODE/requirements.txt"

# --- model ----------------------------------------------------------------------
"$VP" "$HERE/dl.py" --dest "$COMFY_ROOT"

# --- the tools and the chip ------------------------------------------------------
mkdir -p "$HERMES_HOME/tools" "$HERMES_HOME/tts-voices" "$HERMES_HOME/tts-ab" "$HERMES_HOME/cache/scratch"
cp "$HERE"/files/*.py "$HERMES_HOME/tools/"
mkdir -p "$HERMES_HOME/desktop-plugins/dow-voice-chime"
cp "$HERE/files/plugin.js" "$HERMES_HOME/desktop-plugins/dow-voice-chime/plugin.js"

cat > "$HERMES_HOME/tools/tts_pack.json" <<JSON
{
  "hermes_home": "$HERMES_HOME",
  "comfy_root": "$COMFY_ROOT",
  "scratch": "$HERMES_HOME/cache/scratch",
  "share_log": "$HERMES_HOME/cache/scratch/share_path.log",
  "model_dir": "$COMFY_ROOT/ComfyUI/models/checkpoints/Ming-3.0-tts-0.5b-int8-convrot"
}
JSON

# --- the loader hook ------------------------------------------------------------
"$VP" "$HERE/files/apply_share_hook.py" --node "$NODE"

# --- smoke test -----------------------------------------------------------------
export HERMES_HOME COMFY_ROOT
export MING_SHARE_LOG="$HERMES_HOME/cache/scratch/share_path.log"
echo; echo "== booting the pool (cold start ~60 s; it warms the clone path so the first reply is quick)"
"$VP" "$HERMES_HOME/tools/dow_tts.py" pool up || true
n=0; for p in 8188 8189 8190 8191 8192 8193 8194; do curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$p/system_stats" && n=$((n+1)); done
echo "workers answering: $n"
echo "if that is 0, the port that works is the port to fix: check $HERMES_HOME/cache/scratch/weightd_err.log"
echo
echo "Next steps:"
echo "  $VP $HERMES_HOME/tools/dow_tts.py voices"
echo "  $VP $HERMES_HOME/tools/cap_probe.py            # re-measure; the shipped COEF is Windows data"
echo "  add a voice: ref.wav (ONE continuous clip) + ref_text.txt (verbatim transcript of that audio)"
