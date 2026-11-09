"""Reply spooler for the dow-voice-chime plugin: synthesize every chunk, then splice ONE
ordered audio stream and play it with a single mouth (no overlapping players, no window spam).

  start --voice V --b64 TEXT   spawn detached spooler (returns in <1 s; that is the only
                               shell.exec the plugin makes per reply)
  run   --voice V --b64 TEXT   the spooler body: chunk -> stylize -> synth ALL chunks
                               concurrently -> concat in order with pauses -> per-voice
                               tempo match -> play (blocking, one process)
  cancel                       bump the generation token so the running spooler aborts
  calibrate --voice V          measure the synth's own rate vs the game clip's speaking
                               rate and store voice.json["atempo"] for that character

0 extra consoles on Windows (CREATE_NO_WINDOW rides inside the shim's SPAWN_DETACH; POSIX
run-children get start_new_session). Overlap: impossible - one player at a time, owned by the
playlist loop alone (winsound's single in-process device slot on Windows, one tracked player
pid on POSIX). Ordering: chunks are concatenated in index order.
"""
import argparse, base64, json, os, re, subprocess, sys, threading, time, urllib.request, uuid

# python_embeded's ._pth leaves the script directory off sys.path, so the shim and the sibling
# helpers have to be made importable explicitly (the venv python would find them anyway).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stack_paths as sp
import tts_pool

HERMES = sp.HERMES_HOME
VOICES = sp.VOICES
TMP = sp.SCRATCH
TOKEN = os.path.join(VOICES, ".generation")
COMFY = ",".join(tts_pool.urls())   # the live pool's ports if it announced them, else pool.json workers; DOW_COMFY still overrides
DEAD = set()

def pool():
    """Live Ming workers. One ComfyUI instance serves ONE prompt at a time, so N instances =
    N parallel decodes; each job only uses ~15% of the GPU, so this is real parallel gain."""
    out = []
    for base in [b.strip() for b in COMFY.split(",") if b.strip()]:
        if base in DEAD:
            continue
        try:
            OPENER.open(base + "/system_stats", timeout=6).read()
            out.append(base)
        except Exception:
            DEAD.add(base)
    if out:
        return out
    # Nothing answered. The plugin starts the shared-weight pool as soon as a voice is
    # chosen, and a cold holder takes ~60 s to serve, so wait briefly instead of firing
    # prompts at ports that do not exist yet - a silently missing reply is the worse
    # failure. Only wait when a holder is actually alive, so a genuinely dead pool still
    # fails fast.
    try:
        import dow_tts
        alive = bool(dow_tts.holder_pid())
    except Exception:
        alive = False
    if alive:
        for _ in range(12):
            time.sleep(5)
            DEAD.clear()
            for base in [b.strip() for b in COMFY.split(",") if b.strip()]:
                try:
                    OPENER.open(base + "/system_stats", timeout=6).read()
                    out.append(base)
                except Exception:
                    pass
            if out:
                return out
    return out or ["http://127.0.0.1:8188"]
COMFY_IN = os.path.join(sp.COMFY_ROOT, "ComfyUI", "input")
COMFY_OUT = os.path.join(sp.COMFY_ROOT, "ComfyUI", "output")
MING_MODEL = "Ming-3.0-tts-0.5b-int8-convrot"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MAX_CHUNK = 320
MAX_CHUNKS = 10
GAP_MS = 280


def tok():
    try:
        return open(TOKEN, encoding="utf8").read().strip()
    except Exception:
        return ""


def set_tok(v):
    os.makedirs(VOICES, exist_ok=True)
    open(TOKEN, "w", encoding="utf8").write(str(v))


# ~160 characters is ~16 s of speech in one generation. The user hears the audio decay past
# that ("after 160 the decay becomes very noticable"), so a chunk that long is cut at the
# nearest comma/semicolon/dash rather than run as one decode. Env still overrides.
HARD_CHUNK = int(os.environ.get("DOW_MAX_CHUNK", "160"))


def chunks(text):
    """ONE SENTENCE per chunk. The 280 ms splice gap then always falls on a real sentence
    boundary instead of mid-phrase, and short sentences keep each decode short. Decimals
    ("163.6 gigabytes") and abbreviations ("e.g.") are protected so the dot does not split,
    and anything longer than HARD_CHUNK (160 by default, ~16 s of speech) is cut at the
    nearest comma; with no comma in the back half of the window it falls back to a space."""
    clean = re.sub(r"\s+", " ", text).strip()
    # a stylizer catch appended after a sentence that already ended in "." leaves " . " behind
    clean = re.sub(r"(?<=[.!?…])\s+\.(?=\s|$)", "", clean)
    prot = re.sub(r"(?<=\d)\.(?=\d)", "", clean)
    prot = re.sub(r"(?i)(e\.g|i\.e|etc|dr|mr|mrs|ms|st|no|vs|approx|fig)\.",
                  lambda m: m.group(0)[:-1] + "", prot)
    parts = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", prot) if x.strip()]
    out = []
    for x in parts:
        while len(x) > HARD_CHUNK:
            head = x[:HARD_CHUNK]
            cut = max([head.rfind(c) for c in ",;:—–"] + [-1])
            punc = cut >= HARD_CHUNK // 2                 # a real clause boundary near the cap
            if not punc:
                cut = head.rfind(" ")                     # otherwise break on a word boundary
            if cut < 1:
                cut = HARD_CHUNK
            # Keep the comma with the head. Cutting before it left the tail starting with
            # stray punctuation (", and the whole install needs…"), which Ming speaks as an
            # odd leading pause; the comma belongs to the clause that ends there anyway.
            head_txt, rest = (x[:cut + 1], x[cut + 1:]) if punc else (x[:cut], x[cut:])
            out.append(head_txt.strip().replace("", "."))
            x = rest.strip().lstrip(" ,;:—–")
        if x:
            out.append(x.replace("", "."))
    # A character catch ("Attention, brothers."), a one-word reply ("Done.") or a doubled
    # period must not pay for a whole decode pass, and "Done." spoken as its own clip sounds
    # broken - fold anything tiny or letterless into its neighbour.
    res = []
    for x in out:
        tiny = len(x) < 25 or not re.search(r"[A-Za-z]", x)
        if res and tiny and len(res[-1]) + 1 + len(x) <= HARD_CHUNK:
            res[-1] = (res[-1] + " " + x).strip()
        else:
            res.append(x)
    if len(res) > 1 and res[0] and (len(res[0]) < 25 or not re.search(r"[A-Za-z]", res[0]))             and len(res[0]) + 1 + len(res[1]) <= HARD_CHUNK:
        res[1] = (res[0] + " " + res[1]).strip()
        res.pop(0)
    if os.environ.get("DOW_GROUP"):   # opt back into packing whole sentences up to HARD_CHUNK:
        # one decode pass per sentence costs ~10 s of fixed overhead, so grouping is up to ~1.5x
        # faster on short replies at the price of fewer guaranteed pause points - and it must not
        # exceed the length where the user hears decay, so it shares the same cap.
        packed, cur = [], ""
        for x in res:
            if cur and len(cur) + 1 + len(x) > HARD_CHUNK:
                packed.append(cur.strip()); cur = ""
            cur += (" " if cur else "") + x
        if cur.strip():
            packed.append(cur.strip())
        res = packed
    return res[: int(os.environ.get("DOW_MAX_CHUNKS", "24"))]


def stylize(text, meta, first):
    out = text
    for a, b in (meta.get("swaps") or {}).items():
        out = re.sub(r"\b%s\b" % re.escape(a), b, out, flags=re.I)
    # `flavor` is the chip's switch: the character catch (start of the reply) and the persona
    # sentence (end of it). stylize() runs on the whole text BEFORE it is chunked, so the style
    # line lands in the LAST chunk, not next to the catch. The swaps above stay applied either
    # way - they are the accent, not flavour text.
    flavor = tts_pool.settings().get("flavor", True)
    if flavor and first and meta.get("catch"):
        out = meta["catch"] + " " + out
    if flavor and first and meta.get("style"):
        out = out.rstrip(" .!?") + ". " + meta["style"]
    return out


def load_voice(vid):
    d = os.path.join(VOICES, vid)
    ref = os.path.join(d, "ref.wav")
    meta = {}
    if os.path.isfile(os.path.join(d, "voice.json")):
        meta = json.load(open(os.path.join(d, "voice.json"), encoding="utf8"))
    rt = os.path.join(d, "ref_text.txt")
    return ref, (open(rt, encoding="utf8").read().strip() if os.path.isfile(rt) else ""), meta


def ff(args, timeout=120):
    return subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + args, capture_output=True, text=True, timeout=timeout)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        path.replace("\\", "/")], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


EDGE_FLOOR = os.environ.get("DOW_EDGE_FLOOR", "-45dB")


def trim_edges(path, idx):
    """A generated clip carries its own leading and trailing silence, so with one sentence per
    chunk the gap that was heard measured ~0.9 s: the 280 ms pause PLUS both neighbours' dead air.
    Cut each clip's edges (keeping 50 ms) so the pause you hear is the pause that was asked for."""
    if os.environ.get("DOW_EDGES_OFF"):
        return path
    outp = os.path.join(TMP, "spool_edge_%d.wav" % idx)
    af = ("silenceremove=start_periods=1:start_duration=0.05:start_threshold=%s:detection=peak,"
          "areverse,silenceremove=start_periods=1:start_duration=0.05:start_threshold=%s:detection=peak,"
          "areverse") % (EDGE_FLOOR, EDGE_FLOOR)
    ff(["-i", path.replace("\\", "/"), "-af", af, "-c:a", "pcm_s16le", outp.replace("\\", "/")])
    if not os.path.isfile(outp) or os.path.getsize(outp) < 4000:
        return path
    if duration(outp) < max(0.4, 0.35 * duration(path)):   # the trim ate too much: keep the original
        return path
    return outp


def finish_item(wav, idx, atempo):
    """Per-item post-processing done by the WORKER thread, so the player never waits on ffmpeg:
    edge silence off both ends, then this character's measured tempo. Doing atempo per item also
    keeps the 280 ms sentence gaps exact instead of stretching the gaps along with the speech."""
    pth = trim_edges(wav, idx)
    if abs(atempo - 1.0) > 0.02:
        o = os.path.join(TMP, "spool_item_%d.wav" % idx)
        ff(["-i", pth.replace("\\", "/"), "-filter:a", "atempo=%.3f" % atempo,
            "-c:a", "pcm_s16le", o.replace("\\", "/")])
        if os.path.isfile(o) and os.path.getsize(o) > 4000:
            return o
    return pth


def ming_inputs(text, rtext, lang, meta=None):
    """clone = reference audio + exact transcript (tightest timbre lock).
    freeform = prose delivery instruction + optional reference audio (no transcript needed,
    which is the route that works for clipped/vocoded recordings).

    `task` and `instruction` are readable from voice.json, because the need is per voice actor,
    not global: the Venerable Dreadnought's reference (its transcripts confirmed correct against
    the game, 8-13 chars/s, complete sentences) renders as speech-shaped noise under clone and
    speaks the same sentence cleanly under freeform - measured on the SAME reference and the SAME
    83-char sentence: clone 0.10, freeform 0.99. Nothing about that voice's text was wrong."""
    meta = meta or {}
    # precedence, stated once because it is how these knobs get A/B-ed: the environment is the
    # experiment, voice.json is the permanent per-voice setting, the literal is the default.
    task = os.environ.get("DOW_TASK") or meta.get("task") or "clone"
    ins = {"model": ["2", 0], "text": text, "task": task, "seed": 7, "cfg": float(os.environ.get("DOW_CFG", "2.0")),
           "flow_steps": int(os.environ.get("DOW_FLOW_STEPS", "16")), "sigma": 0.25, "temperature": 0.0,
           "max_steps": int(os.environ.get("DOW_MAX_STEPS", "250")), "trim": True}
    if task == "freeform":
        ins["task.instruction"] = (os.environ.get("DOW_INSTR") or meta.get("instruction") or
                                   "Natural, expressive delivery matching the character of the reference recording.")
        ins["task.reference_audio"] = ["1", 0]
    else:
        ins["task.reference_audio"] = ["1", 0]
        ins["task.reference_text"] = rtext
        ins["task.language"] = lang
    return ins


def submit(text, ref, rtext, lang, base, meta=None):
    """Queue one Ming clone job; returns the ComfyUI output wav path (blocking on that job)."""
    rel = "dow_voices/" + os.path.basename(os.path.dirname(ref)) + ".wav"
    dst = os.path.join(COMFY_IN, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(ref):
        import shutil
        shutil.copyfile(ref, dst)
    cid = uuid.uuid4().hex[:8]
    graph = {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": rel}},
        "2": {"class_type": "MingLoadModel", "inputs": {"model": MING_MODEL,
                                                        "precision": tts_pool.graph_precision(),
                                                        "device": "auto", "download_if_missing": False}},
        "3": {"class_type": "MingGenerate", "inputs": ming_inputs(text, rtext, lang, meta)},
        "4": {"class_type": "SaveAudioAdvanced", "inputs": {"audio": ["3", 0],
                                                            "filename_prefix": "dow_spool/" + cid, "format": "flac"}},
    }
    req = urllib.request.Request(base + "/prompt",
                                 data=json.dumps({"prompt": graph, "client_id": str(uuid.uuid4())}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        pid = json.loads(OPENER.open(req, timeout=60).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf8", "ignore")
        raise RuntimeError("comfy 400: " + re.sub(r"\s+", " ", body)[:400])
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(1.0)
        h = json.loads(OPENER.open(base + "/history/" + pid, timeout=20).read())
        if pid in h:
            st = h[pid].get("status") or {}
            if st.get("status_str") == "error":
                raise RuntimeError("comfy: " + json.dumps(st.get("messages"))[:160])
            for o in (h[pid].get("outputs") or {}).values():
                for a in o.get("audio", []):
                    src = os.path.join(COMFY_OUT, a.get("subfolder", ""), a["filename"])
                    wav = os.path.join(TMP, "spool_%s_%s.wav" % (cid, a["filename"].split(".")[0]))
                    if os.path.isfile(src):
                        ff(["-i", src.replace("\\", "/"), "-ar", "44100", "-ac", "1", wav.replace("\\", "/")])
                        return wav
            raise RuntimeError("comfy produced no audio")
    raise RuntimeError("comfy timeout")


def run(vid, text, mytok):
    ref, rtext, meta = load_voice(vid)
    parts = chunks(text if os.environ.get("DOW_NOSTYLE") else stylize(text, meta, True))
    if os.environ.get("DOW_DEBUG"):
        for i, q in enumerate(parts):
            print("chunk %d (%d chars): %s" % (i, len(q), q), flush=True)
    if not parts:
        print("ok 0 none")
        return
    t0 = time.time()
    out = [None] * len(parts)
    # bookkeeping for the playlist: None = still generating, False = failed/unplayable,
    # str = finished file ready to play. The player reads ONLY out[i], never ComfyUI.
    errs = []
    atempo = float(meta.get("atempo") or 1.0)
    workers = pool()
    if not os.environ.get("DOW_NOTRIM"):   # trim by default: Strata re-warms from disk in seconds,
        # and a mid-generation page fault costs far more than a re-read. Ceiling-based, so its
        # warm pages stay: free exactly what the batch needs plus 1 GB.
        import subprocess
        mt = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mem_trim.py")
        try:
            subprocess.run([sys.executable, mt, "--proc", os.environ.get("DOW_TRIM_PROC", "strata"),
                            # measured ~7 GB private commit per Ming worker, so the batch needs
                            # 8 GB per live worker, not the 5 GB this used to assume
                            "--needed", os.environ.get("DOW_TRIM_NEEDED_MB", str(8000 * len(workers))),
                            "--margin", "1024"], timeout=90)
        except Exception as e:
            print("trim skipped: %s" % e, flush=True)

    lk = threading.Lock()
    slot = [0]

    def next_job():
        with lk:
            if slot[0] >= len(parts):
                return None
            i = slot[0]
            slot[0] += 1
            return i

    def worker_loop(base):
        """A free worker takes the NEXT sentence instead of a pre-assigned stripe, so one slow
        generation cannot strand a worker while another one sits idle."""
        while True:
            i = next_job()
            if i is None:
                return
            if tok() != mytok and not os.environ.get("DOW_NOQUIT"):
                with lk:
                    out[i] = False
                return
            try:
                w = submit(parts[i], ref, rtext, meta.get("language", "English"), base, meta)
                with lk:
                    out[i] = finish_item(w, i, atempo)
                print("item %d ready +%.1fs" % (i, time.time() - t0), flush=True)
            except Exception as e:
                errs.append("item%d: %s" % (i, str(e)[:120]))
                with lk:
                    out[i] = False

    ths = [threading.Thread(target=worker_loop, args=(b,)) for b in workers]
    [x.start() for x in ths]

    # ---- ordered playlist: item i plays only when item i-1 has finished playing AND item i
    # exists. If i is still being generated the playlist WAITS on that slot - it never skips it,
    # never overlaps, never starts two at once. Speech starts as soon as the FIRST sentence is
    # ready instead of after the whole reply. DOW_PLAYALL=1 restores wait-for-all-then-play.
    gap = GAP_MS / 1000.0
    spoken, first_at, played = 0.0, None, 0
    # Single player owner: this loop is the only code that starts or stops audio. On Windows
    # `player` stays None - winsound holds the one device slot in-process, and sp.stop_play()
    # is exactly the SND_PURGE the old inline code called. On POSIX it is the one player child
    # (ffplay/pw-play/paplay), SIGTERM'd by sp.stop_play(). stop_play immediately before each
    # new play reproduces PlaySound's documented implicit stop: the previous item's tail - the
    # token-poll loop deliberately waits 0.12 s less than the clip's duration - is cut when the
    # next item starts, at the same instant and with the same 280 ms gap on both platforms.
    player = None
    for i in range(len(parts)):
        t_wait = time.time()
        while out[i] is None and (time.time() - t_wait) < 240:
            if tok() != mytok and not os.environ.get("DOW_NOQUIT"):
                print("cancelled while waiting for item %d" % i, flush=True)
                sp.stop_play(player)
                return
            time.sleep(0.25)
        gen_wait = time.time() - t_wait
        path = out[i]
        if not path:
            print("item %d unavailable - playlist stops after %d item(s)" % (i, played), flush=True)
            break
        if i == 0 and os.environ.get("DOW_PLAYALL"):
            while any(o is None for o in out):
                time.sleep(0.3)
        if i:
            time.sleep(gap)
        d = duration(path)
        spoken += d
        if not os.environ.get("DOW_NOPLAY"):
            t_p = time.time()
            sp.stop_play(player)   # purge previous (no-op cold: Windows purges nothing, POSIX
            player = sp.play_wav(path.replace("\\", "/"), interruptible=True)   # starts at None)
            # ASYNC playback + token polling = real barge-in: the off button or a new reply purges
            # this item mid-sentence rather than letting it finish. Same poll window and cadence
            # as the Windows original; on POSIX the purge is the SIGTERM to the player child.
            while time.time() < t_p + max(0.05, d - 0.12):
                if tok() != mytok and not os.environ.get("DOW_NOQUIT"):
                    sp.stop_play(player)
                    print("cancelled during playback of item %d" % i, flush=True)
                    return
                time.sleep(0.1)
        played += 1
        if first_at is None:
            first_at = time.time() - t0
        print("item %d (%.1fs) played at +%.1fs, waited %.1fs for it to generate"
              % (i, d, time.time() - t0, gen_wait), flush=True)
    sp.stop_play(player)
    [x.join(5) for x in ths]

    # archive artifact = exactly what the playlist played, so silence maps and durations still work
    gp = os.path.join(TMP, "spool_gap.wav")
    if not os.path.isfile(gp):
        ff(["-f", "lavfi", "-t", "%.3f" % gap, "-i", "anullsrc=r=44100:cl=mono",
            "-c:a", "pcm_s16le", gp.replace("\\", "/")])
    lst = os.path.join(TMP, "spool_list.txt")
    seq = []
    for w in out:
        if w:
            if seq:
                seq.append(gp)
            seq.append(w)
    with open(lst, "w", encoding="utf8") as f:
        for w in seq:
            f.write("file '%s'\n" % w.replace("\\", "/").replace("'", ""))
    final = os.path.join(TMP, "spool_%s_%d.wav" % (vid, int(t0)))
    ff(["-f", "concat", "-safe", "0", "-i", lst.replace("\\", "/"), "-c", "copy", final.replace("\\", "/")])
    ms = int((time.time() - t0) * 1000)
    print("ok %d %d items, %.1fs spoken, first audio at +%s %s"
          % (ms, played, spoken, ("%.1fs" % first_at) if first_at is not None else "never",
             final.replace("\\", "/")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--voice")
    ap.add_argument("--b64")
    a = ap.parse_args()

    if a.cmd == "start":
        set_tok(int(time.time() * 1000))
        my = tok()
        p = subprocess.Popen([sys.executable, sys.argv[0], "run", "--voice", a.voice, "--b64", a.b64],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **sp.SPAWN_DETACH)
        print("ok %s %d" % (my, p.pid))
        return

    if a.cmd == "run":
        run(a.voice, base64.b64decode(a.b64).decode("utf8", "ignore"), tok())
        return

    if a.cmd == "cancel":
        set_tok(int(time.time() * 1000))
        print("ok cancelled")
        return

    if a.cmd == "calibrate":
        probe = "The archive is verified and the total is one hundred sixty three point six gigabytes."
        ref, rtext, meta = load_voice(a.voice)
        ref_txt = open(os.path.join(VOICES, a.voice, "ref_text.txt"), encoding="utf8").read().strip()
        # Rate the reference over its SPEECH, not its whole length: a reference spliced from many
        # game clips carries the gaps put between them, and counting those as speech drags every
        # multi-clip voice toward a crawl. voice.json carries speech_s when the builder knows it.
        span = meta.get("speech_s") or duration(os.path.join(VOICES, a.voice, "ref.wav"))
        tgt = len(ref_txt) / max(span, 1.0)

        def measure():
            w = submit(probe, ref, rtext, meta.get("language", "English"), pool()[0], meta)
            d = duration(w)
            return len(probe) / max(d, 1.0), d

        cur, pd = measure()
        # Both ways this measurement goes wrong are silent:
        #  (a) the probe clip comes out TRUNCATED - 2.4 s for 85 chars reads 31 chars/s and pins
        #      the voice at the low clamp forever; a clip that RAMBLES (long references can make
        #      the model pad with pauses/repeats) reads far too slow and pins the high clamp;
        #  (b) the reference is too short to carry a pace at all - a 1.7 s bark with a two-word
        #      transcript measures ~10 chars/s for reasons that have nothing to do with the voice.
        # 85 chars is 3.4-10.6 s at any credible 8-25 chars/s; 8 s / 40 chars is the reference
        # floor. Outside either, borrow the tempo of the actor's other voices instead of inventing
        # one from a bad measurement.
        lo, hi = len(probe) / 25.0, len(probe) / 8.0
        if not (lo <= pd <= hi):
            print("probe clip %.2f s outside %.1f-%.1f s for %d chars: retrying once"
                  % (pd, lo, hi, len(probe)), flush=True)
            cur, pd = measure()
        ok_probe, ok_ref = lo <= pd <= hi, (span >= 8.0 and len(ref_txt) >= 40)
        if not (ok_probe and ok_ref):
            actor = meta.get("actor") or ""
            fam = []
            for d in sorted(os.listdir(VOICES)):
                if d == a.voice or not os.path.isdir(os.path.join(VOICES, d)):
                    continue
                try:
                    m2 = json.load(open(os.path.join(VOICES, d, "voice.json"), encoding="utf8"))
                except (OSError, ValueError):
                    continue
                if actor and m2.get("actor") == actor and m2.get("speech_s", 0) >= 8.0 \
                        and "atempo" in m2:
                    fam.append(m2["atempo"])
            why = ("reference too short to measure (%.1f s speech, %d chars)" % (span, len(ref_txt))
                   if not ok_ref else "probe clip not credible (%.2f s)" % pd)
            f = round(sorted(fam)[len(fam) // 2], 3) if fam else 1.0
            meta["atempo"] = f
            meta["atempo_note"] = ("not measured: %s; %s%.3f from %d measured sibling voice(s) of "
                                   "the same actor" % (why, "borrowed " if fam else "defaulted to ",
                                                       f, len(fam)))
            json.dump(meta, open(os.path.join(VOICES, a.voice, "voice.json"), "w",
                                 encoding="utf8"), indent=1)
            print("skip-measure %s: %s -> atempo %.3f" % (a.voice, meta["atempo_note"], f))
            return
        raw = tgt / max(cur, 0.01)            # <1 = slow the synth down toward the clip pace
        f = max(0.75, min(1.30, 1.0 + 0.55 * (raw - 1.0)))   # damp so no character gets an extreme stretch
        meta["rate_ratio"] = round(raw, 3)
        meta["atempo"] = round(f, 3)   # edit by hand to taste
        meta["atempo_note"] = ""
        meta["rate_ref"] = round(tgt, 2)
        meta["rate_synth"] = round(cur, 2)
        json.dump(meta, open(os.path.join(VOICES, a.voice, "voice.json"), "w", encoding="utf8"), indent=1)
        print("ok %s ref=%.2f synth=%.2f atempo=%.3f" % (a.voice, tgt, cur, f))
        return

    print("unknown cmd")


main()
