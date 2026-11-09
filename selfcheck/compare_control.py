"""Bit-exact verdict for the port regression.

argv[1] = rendered wav, argv[2] = expected md5.
Compares BYTES. Prints spoken seconds of the FILE (not the pipeline's item-sum,
which excludes the 280 ms gap clips and reads ~0.5 s short on a 3-chunk reply).
Exits 0 on an exact match, 1 otherwise, so the calling script's exit code is the verdict.
"""
import hashlib
import os
import sys
import wave

WANT_BYTES = 834006
p, want = sys.argv[1], sys.argv[2]
data = open(p, "rb").read()
got = hashlib.md5(data).hexdigest()
try:
    w = wave.open(p)
    dur = w.getnframes() / float(w.getframerate())
    fmt = "%d ch %d Hz %d bit" % (w.getnchannels(), w.getframerate(), w.getsampwidth() * 8)
except Exception as e:  # not a readable wav -> still judge by bytes
    dur, fmt = -1.0, "unreadable: %s" % e
print("   pack render : %.2f s  %d bytes  md5 %s  (%s)" % (dur, len(data), got, fmt))
print("   control     : 9.45 s  %d bytes  md5 %s" % (WANT_BYTES, want))
if len(data) != WANT_BYTES:
    print("   RESULT      : DIFFERENT - byte length differs, so the chunking/trim/concat changed")
    sys.exit(1)
if got == want:
    print("   RESULT      : BIT-EXACT - the port changed nothing")
    sys.exit(0)
print("   RESULT      : DIFFERENT - same length, different audio; the port changed behaviour")
sys.exit(1)
