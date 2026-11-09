"""Free commit / GPU headroom read, through the SAME shim the pool uses.

argv[1] = the tools dir whose stack_paths should be imported (live or sandbox pack copy).
Prints one short line. Exists so the regression script never has to hand a
`python -c "..."` blob to a native exe (the thing that silently broke three runs:
an MSYS path in the blob became C:\c\... and nothing executed).
"""
import os
import sys

tools = sys.argv[1]
sys.path.insert(0, tools)
import stack_paths as sp  # noqa: E402
import tts_pool  # noqa: E402

gpu = tts_pool._gpu_gb()
mem = tts_pool._avail_gb()
try:
    mem_s = "%.1f GB" % float(mem[1])
except Exception:
    mem_s = repr(mem)
gpu_s = "%.1f GB" % float(gpu) if isinstance(gpu, (int, float)) and gpu >= 0 else repr(gpu)
print("gpu used %s   free commit %s   (shim: %s)" % (gpu_s, mem_s, tools))
