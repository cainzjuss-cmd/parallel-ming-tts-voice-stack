#!/usr/bin/env python3
"""Fetch the file items in download_manifest.json: resume, verify, skip-if-correct.

Only kind=="file" items are fetched here; git items belong to the installer so that the clone
and its checkout stay in one place. A missing/short download is the single most common way this
stack fails confusingly, because ComfyUI will happily boot with a half-written checkpoint and
then die at load time, so verification is not optional: size first (cheap), sha256 second.

  python dl.py                      # everything required
  python dl.py --only ming_05b_int8_convrot
  python dl.py --dest ~/ComfyUI     # overrides {comfy_root}
  python dl.py --force              # re-download even if already verified
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "download_manifest.json")
CHUNK = 1 << 20


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def expand(dest, root):
    return dest.replace("{comfy_root}", root).replace("~", os.path.expanduser("~"))


def already_good(path, item):
    if not os.path.isfile(path):
        return False
    want = item.get("size_bytes")
    if want and os.path.getsize(path) != want:
        return False
    want_sha = item.get("sha256")
    if want_sha:
        got = sha256_of(path)
        if got.lower() != want_sha.lower():
            print("   sha256 mismatch on the existing file (%s != %s), refetching" % (got[:16], want_sha[:16]))
            return False
        return True
    print("   present, size ok, but the manifest carries no sha256 for this item - trusting size only")
    return True


def fetch(url, dest, resume=True):
    """curl when available (it resumes and follows HF redirects properly), urllib otherwise."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    if shutil.which("curl"):
        cmd = ["curl", "-L", "--fail", "--retry", "5", "--retry-delay", "2", "-o", part]
        if resume and os.path.isfile(part):
            cmd.append("-C", "-")
        cmd.append(url)
        rc = subprocess.call(cmd)
        if rc != 0:
            print("   curl failed (rc=%d) - the partial file stays for a later resume" % rc)
            return False
    else:
        start = os.path.getsize(part) if (resume and os.path.isfile(part)) else 0
        req = urllib.request.Request(url, headers={"Range": "bytes=%d-" % start} if start else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(part, "ab" if start else "wb") as out:
                got = start
                total = (start + int(resp.headers.get("Content-Length") or 0)) or None
                while True:
                    blk = resp.read(CHUNK)
                    if not blk:
                        break
                    out.write(blk)
                    got += len(blk)
                    if total:
                        print("   %6.1f%% (%d/%d)" % (100.0 * got / total, got, total), end="\r", flush=True)
                print()
        except Exception as exc:
            print("   urllib failed: %s (partial file kept)" % exc)
            return False
    os.replace(part, dest)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", help="value substituted for {comfy_root}")
    ap.add_argument("--only", action="append", default=[], help="item id (repeatable)")
    ap.add_argument("--include-optional", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-verify", action="store_true", help="skip sha256 (NOT recommended)")
    a = ap.parse_args()

    man = json.load(open(MANIFEST, encoding="utf8"))
    root = a.dest or os.environ.get("COMFY_ROOT") or os.path.expanduser("~/ComfyUI")
    print("comfy_root = %s" % root)
    failures = []
    for item in man["items"]:
        if item["kind"] != "file":
            continue
        if a.only and item["id"] not in a.only:
            continue
        if item.get("required") is False and not (a.include_optional or (a.only and item["id"] in a.only)):
            print("skip (optional): %s" % item["id"])
            continue
        dest = expand(item["dest"], root)
        print("== %s\n   -> %s" % (item["id"], dest))
        if not a.force and already_good(dest, item):
            print("   already verified, nothing to do")
            continue
        if not fetch(item["url"], dest):
            failures.append(item["id"])
            continue
        if a.no_verify:
            print("   fetched, verification skipped by request")
            continue
        size = os.path.getsize(dest)
        if item.get("size_bytes") and size != item["size_bytes"]:
            print("   SIZE WRONG: got %d want %d" % (size, item["size_bytes"]))
            failures.append(item["id"] + " (size)")
            continue
        if item.get("sha256"):
            got = sha256_of(dest)
            if got.lower() != item["sha256"].lower():
                print("   SHA256 WRONG: got %s want %s" % (got, item["sha256"]))
                failures.append(item["id"] + " (sha256)")
                continue
            print("   verified: size %d, sha256 %s" % (size, got[:24] + "..."))
        else:
            print("   fetched, size %d; manifest has no hash for this item so it is UNVERIFIED" % size)
    if failures:
        print("\nFAILED: " + ", ".join(failures))
        return 1
    print("\nall requested file items are in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
