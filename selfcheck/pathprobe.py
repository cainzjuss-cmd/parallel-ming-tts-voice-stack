"""Non-disruptive precedence probe: does the PACK copy resolve to the sandbox
home, or to this box's live home?  Run BEFORE the regression touches the pool.
Nothing here starts, stops or renders anything."""
import os
import sys

SB = r"C:/Users/Sander/tts-stack-pack/testhome"
sys.path.insert(0, os.path.join(SB, "tools"))
import stack_paths as sp  # noqa: E402
import tts_pool  # noqa: E402

roots = {
    "HERMES_HOME": sp.HERMES_HOME,
    "TOOLS_DIR": sp.TOOLS_DIR,
    "SCRATCH": sp.SCRATCH,
    "VOICES": sp.VOICES,
    "COMFY_ROOT": sp.COMFY_ROOT,
    "MODEL_DIR": sp.MODEL_DIR,
    "SHARE_LOG": sp.SHARE_LOG,
    "pool.json": tts_pool.SETTINGS,
    "actual": tts_pool.ACTUAL,
}
bad = []
for k, v in roots.items():
    print("   %-12s %s" % (k, v))
    if "AppData" in v.replace("\\", "/").split("/tts-stack-pack")[0] and k != "COMFY_ROOT":
        bad.append((k, v))
    if not os.path.isdir(v.replace("\\", "/").rsplit("/", 1)[0]) and k != "actual":
        print("      ^ parent missing on disk")
LIVE = r"C:/Users/Sander/AppData/Local/hermes"
print("   CFG file used:", sp.CFG if not sp.CFG else "<read: %s>" % os.path.join(SB, "tools", "tts_pack.json"))
print("   OS=%s SPAWN_DETACH=%s" % (sp.OS, sp.SPAWN_DETACH))
print("   pool settings:", tts_pool.settings())
cap = tts_pool.capacity() if hasattr(tts_pool, "capacity") else None
print("   VERDICT:", "LIVE ROOTS LEAKING -> fix precedence first" if bad else "sandbox roots, no live leak")
for k, v in bad:
    print("     leak:", k, v)
sys.exit(1 if bad else 0)
