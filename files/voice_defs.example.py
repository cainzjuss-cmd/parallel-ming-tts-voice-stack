"""Schema for voice_defs.py -- copy this to voice_defs.py and fill it in.

build_dow_voices.py imports ORK (an optional shared swap table) and VOICES (a list of
tuples). One tuple per voice, one voice per voice ACTOR:

  id             folder name under tts-voices/  (<=32 chars, [A-Za-z0-9_-] only -- the
                 desktop chip drops anything else)
  name           what the chip shows
  tag            <=3 chars shown on the chip button itself
  source_folder  folder in your local reference tree holding this actor's clips
  session_prefix filename prefix inside that folder
  catch          line SPOKEN before the first sentence of every reply ("" = none)
  style          line SPOKEN after the last sentence of every reply ("" = none)
  swaps          word-level substitutions applied to the reply text before synthesis

catch/style are literal speech, not instructions to a model, so they must be lines the
voice can actually say and that you are willing to hear on every reply. Keep anything
quoted from a game or a real person out of a public repo: this example is placeholder
text of my own.
"""

ORK = {"the": "da", "this": "dis", "that": "dat", "with": "wif", "and": "an'", "you": "yoo"}

VOICES = [
    ("example_voice", "Example Voice", "EXV", "00_example_actor", "session_00",
     "Opener line here.", "Closer line here.",
     {"very": "really", "immediately": "at once"}),
]
