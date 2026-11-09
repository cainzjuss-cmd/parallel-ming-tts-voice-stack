#!/usr/bin/env python3
"""Install (or re-install) the MING_SHARE hook into ComfyUI-Ming-3-tts/loader.py.

The shared-weight pool only works if the worker process skips its own checkpoint load and binds
the tensors the holder already owns. That needs a hook inside the node's load_model(). A node
update overwrites the file, so this script exists to be run again after every update - it is
idempotent and it refuses loudly instead of guessing when the upstream shape has changed.

  python apply_share_hook.py --node <path-to-ComfyUI-Ming-3-tts> [--dry-run] [--revert]

Why it aborts rather than patching near-miss anchors: a wrong insertion inside a model loader is
a silent-corruption class of bug (wrong audio, no exception). If the file does not look exactly
like the version this hook was written for, a human should look at it.
"""
import argparse
import os
import re
import sys

BLOCK = '''    # --- MING_SHARE hook (added by apply_share_hook.py; safe to re-run) -------------------
    # When a shared-weight holder has already mapped the model in this process, bind those
    # tensors instead of reading the checkpoint again. Skipping the load is safe for
    # comfy_quant metadata: it is consulted only inside _load_from_state_dict
    # (comfy/ops.py ~1285/1660/1772), never at runtime, and its effect travels inside the
    # QuantizedTensor objects the payload carries. MING_KEEP_LOAD=1 restores the upstream
    # load-then-swap order for an A/B or for recovery if a quant library changes underneath.
    _shared = None
    if os.environ.get("MING_SHARE") and os.environ.get("MING_KEEP_LOAD") != "1":
        try:
            import ming_worker
            _shared = getattr(ming_worker, "SHARED", None)
        except Exception as _exc:
            raise RuntimeError("MING_SHARE is set but ming_worker did not import (is the TTS "
                               "tools directory on PYTHONPATH?): %s" % _exc)
    if _shared is not None:
        _bound, _bad = ming_worker.unpack(_shared, model)
        # ComfyUI's stdout tee swallows prints from inside a node, so the bind path is written
        # to a file when MING_SHARE_LOG is set. Without it there is no evidence of which route
        # a given boot took, and both routes produce audio.
        _log = os.environ.get("MING_SHARE_LOG")
        if _log:
            with open(_log, "a", encoding="utf8") as _fh:
                _fh.write("%d ZERO-COPY bound=%d bad=%d\\n" % (os.getpid(), _bound, _bad))
    else:
        load_weights(model, path, dtype)
        if os.environ.get("MING_SHARE"):
            _log = os.environ.get("MING_SHARE_LOG")
            if _log:
                with open(_log, "a", encoding="utf8") as _fh:
                    _fh.write("%d KEEP-LOAD private copy then swap\\n" % os.getpid())
            # Kept from the arrangement this box actually runs: when SHARED was not yet published
            # the load happens privately and the holder's tensors are then bound over it. Dropping
            # this half would silently change the MING_KEEP_LOAD=1 escape into a plain per-process
            # copy, which is a different regime from the one the measurements were taken in.
            try:
                import ming_worker
                if getattr(ming_worker, "SHARED", None):
                    ming_worker.unpack(ming_worker.SHARED, model)
            except Exception as _exc:
                raise RuntimeError("MING_KEEP_LOAD swap failed: %s" % _exc)
    # --- end MING_SHARE hook --------------------------------------------------------------'''

MARK = "MING_SHARE hook (added by apply_share_hook.py"
# The upstream line this replaces. Anchored on its own because the model is built on meta and the
# load call is what materialises it; the two are only correct in this order.
ANCHOR = re.compile(r"^(\s*)load_weights\(model, path, dtype\)\s*$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True, help="path to custom_nodes/ComfyUI-Ming-3-tts")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true", help="restore loader.py.orig if we saved one")
    a = ap.parse_args()

    loader = os.path.join(a.node, "loader.py")
    if not os.path.isfile(loader):
        print("not found: %s" % loader)
        return 2
    src = open(loader, encoding="utf8").read()

    if a.revert:
        orig = loader + ".orig"
        if not os.path.isfile(orig):
            print("no backup at %s - nothing to revert (the file may never have been touched)" % orig)
            return 1
        open(loader, "w", encoding="utf8", newline="").write(open(orig, encoding="utf8").read())
        print("reverted loader.py from %s" % orig)
        return 0

    if MARK in src or "MING_SHARE" in src:
        print("a MING_SHARE hook is already present - nothing to do (%s)" % loader)
        print("if that hook was written by hand and you want the scripted version instead, run")
        print("with --revert first (it restores loader.py.orig).")
        return 0

    lines = src.splitlines()
    hits = [(i, m.group(1)) for i, ln in enumerate(lines) for m in [ANCHOR.match(ln)] if m]
    if not hits:
        print("REFUSING: no line matching 'load_weights(model, path, dtype)' in %s" % loader)
        print("The upstream loader changed shape. Apply by hand - see docs/upstream-proposal.md,")
        print("and the hook must go where the meta-built model is materialised, before model.eval().")
        return 3
    if len(hits) > 1:
        print("REFUSING: %s candidate load sites (%s) - an ambiguous anchor could patch the wrong"
              " model branch." % (len(hits), ", ".join("line %d" % (i + 1) for i, _ in hits)))
        print("Pick the one inside load_model() for the 0.5B bundle and apply by hand.")
        return 4

    idx, indent = hits[0]
    if "model.eval()" not in "\n".join(lines[idx + 1:idx + 8]):
        print("REFUSING: the anchor is not followed by model.eval() within 7 lines, so this is not"
              " the spot the hook was written for (found line %d)." % (idx + 1))
        return 5

    block = BLOCK.replace("\n    ", "\n" + indent) if indent != "    " else BLOCK
    out = lines[:idx] + block.splitlines() + lines[idx + 1:]
    if a.dry_run:
        print("dry run: would replace line %d with the %d-line hook:\n" % (idx + 1, len(block.splitlines())))
        print("\n".join(out[max(0, idx - 4):idx + len(block.splitlines()) + 4]))
        return 0
    try:
        compile("\n".join(out), loader, "exec")
    except SyntaxError as exc:
        print("REFUSING: the patched file does not parse (%s)" % exc)
        return 6
    if not os.path.isfile(loader + ".orig"):
        open(loader + ".orig", "w", encoding="utf8", newline="").write(src)
    open(loader, "w", encoding="utf8", newline="\n").write("\n".join(out) + "\n")
    print("hook installed at line %d of %s (backup: loader.py.orig)" % (idx + 1, loader))
    print("reminder: workers need the TTS tools dir on PYTHONPATH and MING_SHARE_LOG set, which the")
    print("holder sets when it spawns them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
