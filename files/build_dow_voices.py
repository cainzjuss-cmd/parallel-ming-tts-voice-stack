"""Build the six Dawn of War reply voices under hermes/tts-voices/ (self-contained on C:).

Reference clips come from the extracted DoW:DE archives (produced by the extraction job) and
are COPIED to the voices root so a drive dropout cannot break the plugin. ref_text.txt is
required by the TTS clone path and comes from the local ASR of that same clip.

Paths resolve through the shim: the extraction tree (asr_transcripts.json + clip folders) is
licensed game material and NOT shipped, so it has no per-OS default - point at it with env
TTS_VOICE_SRC or tts_pack.json "voice_src" (this box pins its O: location there). The output
root is the very same tts-voices directory the runtime reads (TTS_VOICES / voices / default).
Nothing runs at import time, so importing this module on Linux cannot raise.
"""
import json, os, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack_paths as sp

SRC = sp.env("TTS_VOICE_SRC", "voice_src", None)
DST = sp.VOICES

# The persona table is NOT published with this repo. dow_spool.stylize() SINGS these strings:
# "catch" is prefixed to the first sentence of every spoken reply, "style" is appended to the
# last, "swaps" are word-level substitutions applied to the whole reply. For the six characters
# built here that text is quoted from the game's own voice bank, exactly like the reference clips
# it is paired with, so both stay local. The definitions live in voice_defs.py next to this
# script (which each deployment supplies) and voice_defs.example.py documents the schema.
try:
    import voice_defs
except ImportError:
    raise SystemExit(
        "voice_defs.py not found next to build_dow_voices.py.\n"
        "Copy voice_defs.example.py to voice_defs.py and fill in your own table of\n"
        "(id, name, tag, source_folder, session_prefix, catch, style, swaps).")

ORK = voice_defs.ORK
VOICES = voice_defs.VOICES

def main():
    if not SRC or not os.path.isfile(os.path.join(SRC, "asr_transcripts.json")):
        print("no voice source tree: set TTS_VOICE_SRC or tts_pack.json \"voice_src\" to the "
              "directory holding asr_transcripts.json and the extracted reference clips")
        return
    TR = json.load(open(os.path.join(SRC, "asr_transcripts.json"), encoding="utf8"))
    os.makedirs(DST, exist_ok=True)
    made = []
    for vid, name, tag, folder, which, catch, style, swaps in VOICES:
        rel = None
        for f in sorted(os.listdir(os.path.join(SRC, folder))):
            if f.startswith(which + "_") and f.endswith(".wav"):
                rel = folder + "/" + f
                break
        if not rel:
            print("MISSING source for", vid)
            continue
        src_wav = os.path.join(SRC, rel.replace("/", os.sep))
        d = os.path.join(DST, vid)
        os.makedirs(d, exist_ok=True)
        shutil.copyfile(src_wav, os.path.join(d, "ref.wav"))
        text = (TR.get(rel) or "").strip()
        if not text:
            print("NO TRANSCRIPT for", rel)
        open(os.path.join(d, "ref_text.txt"), "w", encoding="utf8").write(text)
        json.dump({"name": name, "tag": tag, "language": "English", "catch": catch,
                   "style": style, "swaps": swaps, "source": rel,
                   "provenance": "DoW Definitive Edition, Relic SGA speech bank, decoded via vgmstream"},
                  open(os.path.join(d, "voice.json"), "w", encoding="utf8"), indent=1)
        made.append((vid, tag, name, os.path.getsize(os.path.join(d, "ref.wav")), len(text)))

    legacy = os.path.join(DST, "test_paradee")
    if os.path.isdir(legacy):
        shutil.rmtree(legacy)
        print("removed placeholder voice test_paradee")

    for row in made:
        print("%-13s %-4s %-26s ref.wav %6d KB  ref_text %d chars" % (row[0], row[1], row[2], row[3] // 1024, row[4]))


if __name__ == "__main__":
    main()
