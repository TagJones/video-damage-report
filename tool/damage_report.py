#!/usr/bin/env python3
"""Pre-existing damage report: narrated walkthrough video -> photo + description PDF.

You film the property, point at each bit of damage and say where it is and what it is.
This tool, running entirely on your own PC:
  1. transcribes your narration with Whisper (with word-level timestamps),
  2. splits it into one item per damage spot (items.json - plain text, editable),
  3. pulls still frames from the video while you were talking about each spot,
     keeps the sharpest ones as candidates and auto-picks the best,
  4. builds the report (HTML + PDF): photo, your own words, and the video timestamp.

Commands:
  all        VIDEO                    do every step in one go
  transcribe VIDEO                    steps 1+2 -> transcript.txt, items.json
  frames     WORKDIR                  step 3    -> frames/item_NN/c*.jpg + contact.jpg
  overview   WORKDIR                  one frame per second of the whole video -> overview.jpg
  grab       WORKDIR --item N TIME    pull a still at a specific time (m:ss or seconds)
  report     WORKDIR                  step 4    -> Damage Report.html / .pdf

WORKDIR is the "<video name> - damage report" folder created next to the video.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace", line_buffering=True)
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
import warnings  # noqa: E402
warnings.filterwarnings("ignore", message=".*unauthenticated requests.*")
warnings.filterwarnings("ignore", message=".*symlinks.*")

ITEMS = "items.json"
TRANSCRIPT = "transcript.json"
REPORT_NAME = "Damage Report"

# Helps Whisper spell the words people use in condition reports.
VOCAB_PROMPT = (
    "Move-in walkthrough of a rental property, noting pre-existing damage. "
    "Scuff, scratch, chip, dent, gouge, stain, crack, hole, nail hole, water damage, "
    "peeling paint, drywall, baseboard, trim, countertop, cabinet, carpet, laminate, "
    "vinyl plank, tile, grout, caulk, window sill, blinds, screen, door frame, hinge."
)


# --------------------------------------------------------------------------- utils

def fmt_ts(t: float, decimals: bool = False) -> str:
    t = max(0.0, float(t))
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    sec = f"{s:04.1f}" if decimals else f"{int(s):02d}"
    return f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}"


def parse_ts(s: str) -> float:
    v = 0.0
    for part in str(s).strip().split(":"):
        v = v * 60 + float(part)
    return v


def workdir_for(video: Path) -> Path:
    return video.parent / f"{video.stem} - damage report"


def load_doc(work: Path) -> dict:
    p = work / ITEMS
    if not p.exists():
        sys.exit(f"No {ITEMS} in {work}. Run 'transcribe' first.")
    return json.loads(p.read_text(encoding="utf-8"))


def save_doc(work: Path, doc: dict) -> None:
    (work / ITEMS).write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def video_path(work: Path, doc: dict) -> Path:
    p = Path(doc["video_path"])
    return p if p.is_absolute() else (work / p).resolve()


def write_jpg(path: Path, img, quality: int = 92) -> None:
    import cv2
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError(f"could not encode {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


def read_img(path: Path):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)


def default_paper() -> str:
    name = os.environ.get("LANG", "")
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
            name = buf.value
        except Exception:
            pass
    letter = ("US", "CA", "MX", "PH", "CL", "CO", "VE", "GT", "CR", "PA", "PR", "SV", "DO")
    region = re.split(r"[-_]", name)[-1].upper() if name else ""
    return "Letter" if region in letter else "A4"


def nice_datetime(d: dt.datetime) -> str:
    return f"{d.day} {d:%b %Y}, {d:%I:%M %p}".replace(", 0", ", ")


# --------------------------------------------------------------------------- video info

def probe_video(path: Path) -> dict:
    import av
    info: dict = {"name": path.name, "size_bytes": path.stat().st_size}
    with av.open(str(path)) as c:
        meta = {k.lower(): v for k, v in c.metadata.items()}
        vs = c.streams.video[0] if c.streams.video else None
        if vs is not None:
            meta.update({k.lower(): v for k, v in vs.metadata.items() if k.lower() not in meta})
            info["width"], info["height"] = vs.codec_context.width, vs.codec_context.height
            if vs.average_rate:
                info["fps"] = round(float(vs.average_rate), 2)
        if c.duration:
            info["duration"] = round(c.duration / 1_000_000, 2)
    apple = meta.get("com.apple.quicktime.creationdate")  # iPhone: local time with offset
    raw = apple or meta.get("creation_time") or meta.get("date")
    if raw:
        info["recorded_raw"] = raw
        try:
            d = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            if not apple and d.tzinfo:
                d = d.astimezone()  # creation_time is UTC on most phones - show this PC's local time
            info["recorded"] = nice_datetime(d)
            info["recorded_date"] = d.date().isoformat()
        except Exception:
            info["recorded"] = raw
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    info["sha256"] = h.hexdigest()
    return info


# --------------------------------------------------------------------------- step 1+2: transcribe + draft items

FILLER_START = re.compile(r"^(?:(?:okay|ok|alright|all right|so|and|now|next|um+|uh+|erm|right)[,.]?\s+)+", re.I)
DAMAGE_PHRASE = re.compile(
    r"\b(there'?s|there is|there are|there was|we'?ve got|i'?ve got|you can see|"
    r"you'?ll see|got a|got some|has a|has some|it'?s got)\b", re.I)
FILLERS = re.compile(r"\b(um+|uh+|erm)\b[,.]?\s*", re.I)
LEAD_IN = re.compile(r"^(?:starting|start|moving on|moving|going|heading|now|next|here|this is|we'?re|i'?m)\b[^.,]*?"
                     r"\b(?:in|into|to|on|at|with|is|are)\s+(?:the\s+)?", re.I)


def cap(s: str) -> str:
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def clean_location(loc: str) -> str:
    """'Starting in the kitchen. On the countertop' -> 'Kitchen - on the countertop'."""
    loc = LEAD_IN.sub("", loc.strip(" ,.-;:"))
    loc = re.sub(r"[.;:]\s+(\w)", lambda m: " - " + m.group(1).lower(), loc)
    return cap(loc)


def split_location(text: str) -> tuple[str, str]:
    """Best-effort draft: 'Kitchen, left of the sink. There's a chip...' -> (location, description)."""
    t = FILLER_START.sub("", FILLERS.sub("", text).strip())
    m = DAMAGE_PHRASE.search(t)
    if m and 1 <= len(t[: m.start()].split()) <= 15:
        return clean_location(t[: m.start()]), cap(t[m.start():])
    parts = re.split(r"(?<=[.!?])\s+", t, maxsplit=1)
    if len(parts) == 2 and len(parts[0].split()) <= 10:
        return clean_location(parts[0]), cap(parts[1])
    return "", cap(t)


def sentences(segments: list[dict]) -> list[tuple[float, float, str]]:
    """Regroup Whisper's word timings into one line per sentence, each with its own start time."""
    groups, cur = [], []
    for s in segments:
        words = s.get("words") or [{"start": s["start"], "end": s["end"], "word": " " + s["text"]}]
        for w in words:
            cur.append(w)
            if re.search(r"[.!?][\"')\]]*$", w["word"].strip()) or len(cur) >= 45:
                groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)
    return [(g[0]["start"], g[-1]["end"], re.sub(r"\s+", " ", "".join(w["word"] for w in g)).strip()) for g in groups]


def draft_items(segments: list[dict], gap: float) -> list[dict]:
    words = [w for s in segments for w in s.get("words", [])]
    if not words:  # no word timings: one item per segment
        groups = [[{"start": s["start"], "end": s["end"], "word": " " + s["text"]}] for s in segments]
    else:
        groups, cur = [], []
        for w in words:
            if cur and w["start"] - cur[-1]["end"] >= gap:
                groups.append(cur)
                cur = []
            cur.append(w)
        if cur:
            groups.append(cur)
    items = []
    for g in groups:
        text = re.sub(r"\s+", " ", "".join(w["word"] for w in g)).strip()
        if not text:
            continue
        loc, desc = split_location(text)
        items.append({
            "id": len(items) + 1,
            "location": loc,
            "description": desc,
            "start": round(g[0]["start"], 2),
            "end": round(g[-1]["end"], 2),
            "photos": [],
            "exclude": len(text.split()) < 3,
            "transcript": text,
        })
    return items


def load_audio(path: Path, sr: int = 16000):
    """Decode the video's audio track to 16 kHz mono float32 (what Whisper expects)."""
    import av
    import numpy as np
    chunks = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            sys.exit("This video has no audio track - the narration is needed to build the report.")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sr)
        for frame in c.decode(c.streams.audio[0]):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def cmd_transcribe(args) -> Path:
    video = Path(args.video).resolve()
    if not video.exists():
        sys.exit(f"Video not found: {video}")
    work = workdir_for(video)
    work.mkdir(exist_ok=True)

    tpath = work / TRANSCRIPT
    if tpath.exists() and not args.force:
        print(f"Using existing transcript ({tpath.name}). Use --force to redo it.")
        tdoc = json.loads(tpath.read_text(encoding="utf-8"))
    else:
        from faster_whisper import WhisperModel
        print(f"Loading Whisper model '{args.model}' (first run downloads it, ~0.5-1.5 GB)...")
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             cpu_threads=max(1, (os.cpu_count() or 4)))
        print("Transcribing (runs on this PC, nothing is uploaded)...")
        segments, info = model.transcribe(
            load_audio(video),
            language=args.language,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 600},
            initial_prompt=VOCAB_PROMPT,
            beam_size=5,
            hallucination_silence_threshold=2.0,
        )
        segs = []
        for s in segments:
            words = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word,
                      "p": round(w.probability, 3)} for w in (s.words or [])]
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(), "words": words})
            pct = f"{100 * s.end / info.duration:3.0f}%" if info.duration else ""
            print(f"  {pct} [{fmt_ts(s.start)}] {s.text.strip()}")
        tdoc = {"video": video.name, "model": args.model, "language": info.language,
                "duration": round(info.duration, 2), "segments": segs}
        tpath.write_text(json.dumps(tdoc, indent=1, ensure_ascii=False), encoding="utf-8")
        lines = [f"[{fmt_ts(a, True)} - {fmt_ts(b, True)}] {text}" for a, b, text in sentences(segs)]
        (work / "transcript.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    items = draft_items(tdoc["segments"], args.gap)
    print("Reading video details and fingerprint (SHA-256)...")
    vinfo = probe_video(video)
    try:
        rel = os.path.relpath(video, work)
    except ValueError:  # different drive
        rel = str(video)
    doc = {
        "video_path": rel,
        "video": vinfo,
        "info": {"title": "Pre-Existing Damage Report", "property": "", "tenant": "", "landlord": "",
                 "inspection_date": vinfo.get("recorded_date", ""), "notes": ""},
        "items": items,
    }
    target = work / ITEMS
    if target.exists() and not args.force:
        (work / "items.draft.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{ITEMS} already exists, so your edits were kept. New draft written to items.draft.json.")
    else:
        save_doc(work, doc)
        n = sum(not i["exclude"] for i in items)
        print(f"Drafted {n} damage items -> {target}")
    return work


# --------------------------------------------------------------------------- step 3: frames

def open_capture(path: Path):
    import cv2
    cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        sys.exit(f"OpenCV could not open the video: {path}")
    if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
        cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)  # honour phone rotation metadata
    return cap


def sharpness(frame) -> float:
    import cv2
    h, w = frame.shape[:2]
    s = 720 / max(h, w)
    small = cv2.resize(frame, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def contact_sheet(cands: list[dict], frames: list, title: str, out: Path, picked: set[str]) -> None:
    import cv2
    import numpy as np
    cell, pad, label_h, head, cols = 400, 10, 30, 48, 4
    rows = max(1, math.ceil(len(cands) / cols))
    W = cols * (cell + pad) + pad
    H = head + rows * (cell + label_h + pad) + pad
    sheet = np.full((H, W, 3), 255, np.uint8)
    ascii_title = title.encode("ascii", "replace").decode()
    cv2.putText(sheet, ascii_title[:110], (pad, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)
    for j, (c, img) in enumerate(zip(cands, frames)):
        r, col = divmod(j, cols)
        h, w = img.shape[:2]
        s = min(cell / w, cell / h)
        tw, th = max(1, int(w * s)), max(1, int(h * s))
        thumb = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        x0 = pad + col * (cell + pad)
        y0 = head + r * (cell + label_h + pad)
        sheet[y0:y0 + cell, x0:x0 + cell] = (235, 235, 235)
        sheet[y0 + (cell - th) // 2: y0 + (cell - th) // 2 + th, x0 + (cell - tw) // 2: x0 + (cell - tw) // 2 + tw] = thumb
        color = (0, 140, 0) if c["id"] in picked else (40, 40, 40)
        if c["id"] in picked:
            cv2.rectangle(sheet, (x0 - 4, y0 - 4), (x0 + cell + 3, y0 + cell + 3), color, 4)
        label = f"{c['id']}   {fmt_ts(c['t'], True)}" + ("   PICKED" if c["id"] in picked else "")
        cv2.putText(sheet, label, (x0 + 4, y0 + cell + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA)
    write_jpg(out, sheet, 85)


def cmd_frames(args, work: Path | None = None) -> Path:
    import cv2
    work = work or Path(args.workdir).resolve()
    doc = load_doc(work)
    video = video_path(work, doc)
    only = {int(x) for x in args.only.split(",")} if getattr(args, "only", None) else None
    all_items = sorted([i for i in doc["items"] if not i.get("exclude")], key=lambda i: i["start"])

    todo = []
    for k, it in enumerate(all_items):
        if only is not None and it["id"] not in only:
            continue
        if only is None and not args.redo and it.get("candidates"):
            continue  # already done - keeps your picks
        a = max(0.0, it["start"] - args.pre)
        b = it["end"] + args.post
        if k + 1 < len(all_items):
            b = min(b, max(it["end"], all_items[k + 1]["start"]))
        if b - a < 0.6:
            b = a + 0.6
        todo.append({"item": it, "a": a, "b": b, "best": [None] * args.candidates, "done": False})
    if not todo:
        print("All items already have candidate stills (use --redo or --only N to redo).")
        return work

    print(f"Scanning video for {len(todo)} item(s)...")
    cap = open_capture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    end_all = max(w["b"] for w in todo)
    step = 1.0 / args.sample_fps
    nb = args.candidates
    last_eval, idx, next_report = -1e9, -1, 0.0

    def flush(w):
        it = w["item"]
        d = work / "frames" / f"item_{it['id']:02d}"
        if d.exists():
            for f in d.glob("c*.jpg"):
                f.unlink()
        kept = [b for b in w["best"] if b is not None]
        cands, imgs = [], []
        for n, (s, t, img) in enumerate(kept, 1):
            cid = f"c{n}"
            write_jpg(d / f"{cid}.jpg", img)
            cands.append({"id": cid, "t": round(t, 2), "sharpness": round(s, 1),
                          "file": (d / f"{cid}.jpg").relative_to(work).as_posix()})
            imgs.append(img)
        it["candidates"] = cands
        it["photos"] = [max(cands, key=lambda c: c["sharpness"])["id"]] if cands else []
        title = f"Item {it['id']}: {it.get('location') or it.get('description', '')[:60]}  ({fmt_ts(it['start'])}-{fmt_ts(it['end'])})"
        if imgs:
            contact_sheet(cands, imgs, title, d / "contact.jpg", set(it["photos"]))
        w["best"] = None
        w["done"] = True

    while True:
        if not cap.grab():
            break
        idx += 1
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t <= 0 and idx > 0:
            t = idx / fps
        if t > end_all + 0.5:
            break
        if t >= next_report:
            print(f"  {fmt_ts(t)} / {fmt_ts(end_all)}", flush=True)
            next_report = t + 60
        for w in todo:
            if not w["done"] and t > w["b"]:
                flush(w)
        active = [w for w in todo if not w["done"] and w["a"] <= t <= w["b"]]
        if not active or t - last_eval < step:
            continue
        ok, frame = cap.retrieve()
        if not ok:
            continue
        last_eval = t
        s = sharpness(frame)
        for w in active:
            k = min(nb - 1, int((t - w["a"]) / (w["b"] - w["a"]) * nb))
            cur = w["best"][k]
            if cur is None or s > cur[0]:
                w["best"][k] = (s, t, frame.copy())
    for w in todo:
        if not w["done"]:
            flush(w)
    cap.release()
    save_doc(work, doc)
    print(f"Candidate stills + contact sheets written to {work / 'frames'}")
    return work


def cmd_overview(args) -> None:
    """One labelled frame every N seconds across the whole video -> overview.jpg (to see what's on camera when)."""
    import cv2
    import numpy as np
    work = Path(args.workdir).resolve()
    doc = load_doc(work)
    cap = open_capture(video_path(work, doc))
    frames, next_t = [], 0.0
    while cap.grab():
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t + 1e-6 >= next_t:
            ok, f = cap.retrieve()
            if ok:
                frames.append((t, f))
                next_t += args.every
    cap.release()
    if not frames:
        sys.exit("No frames could be read from the video.")
    th, cols = 300, 9
    thumbs = []
    for t, f in frames:
        h, w = f.shape[:2]
        im = cv2.resize(f, (max(1, int(w * th / h)), th), interpolation=cv2.INTER_AREA)
        im = cv2.copyMakeBorder(im, 0, 26, 2, 2, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        cv2.putText(im, fmt_ts(t, True), (4, th + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
        thumbs.append(im)
    tw = max(i.shape[1] for i in thumbs)
    thumbs = [cv2.copyMakeBorder(i, 0, 0, 0, tw - i.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255)) for i in thumbs]
    while len(thumbs) % cols:
        thumbs.append(np.full_like(thumbs[0], 255))
    rows = [np.hstack(thumbs[r:r + cols]) for r in range(0, len(thumbs), cols)]
    sheets = [np.vstack(rows[i:i + 4]) for i in range(0, len(rows), 4)]  # 36 frames per image
    for n, sheet in enumerate(sheets, 1):
        out = work / (f"overview_{n:02d}.jpg" if len(sheets) > 1 else "overview.jpg")
        write_jpg(out, sheet, 85)
        print(f"Wrote {out}")


def cmd_grab(args) -> None:
    import cv2
    work = Path(args.workdir).resolve()
    doc = load_doc(work)
    it = next((i for i in doc["items"] if i["id"] == args.item), None)
    if it is None:
        sys.exit(f"No item with id {args.item}")
    t0 = parse_ts(args.time)
    cap = open_capture(video_path(work, doc))
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t0 - 2.0) * 1000)
    best = None
    while cap.grab():
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t < t0 - args.snap:
            continue
        if t > t0 + args.snap:
            break
        ok, frame = cap.retrieve()
        if ok:
            s = sharpness(frame)
            if best is None or s > best[0]:
                best = (s, t, frame.copy())
    cap.release()
    if best is None:
        sys.exit(f"No frame found near {args.time}")
    cands = it.setdefault("candidates", [])
    n = 1 + sum(c["id"].startswith("g") for c in cands)
    cid = f"g{n}"
    f = work / "frames" / f"item_{it['id']:02d}" / f"{cid}.jpg"
    write_jpg(f, best[2])
    cands.append({"id": cid, "t": round(best[1], 2), "sharpness": round(best[0], 1), "file": f.relative_to(work).as_posix()})
    it["photos"] = (it.get("photos") or []) + [cid] if args.add else [cid]
    save_doc(work, doc)
    print(f"Saved {f} at {fmt_ts(best[1], True)}; item {it['id']} photos = {it['photos']}")


# --------------------------------------------------------------------------- step 4: report

CSS = """
@page { size: %(paper)s; margin: 0.55in 0.6in 0.7in;
  @bottom-left { content: "%(footer)s"; font: 8pt "Segoe UI", Arial, sans-serif; color: #7a838c; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font: 8pt "Segoe UI", Arial, sans-serif; color: #7a838c; } }
:root { --ink:#1d2125; --muted:#5d6872; --rule:#d7dce1; --soft:#f4f6f8; --accent:#8c3a1e; }
* { box-sizing: border-box; }
html { background: #e9ecef; }
body { margin: 0 auto; max-width: 8.5in; background: #fff; color: var(--ink);
  font: 10.5pt/1.45 "Segoe UI", Arial, sans-serif; padding: 0.5in 0.6in; }
@media print { html { background: #fff; } body { max-width: none; padding: 0; } }
h1 { font-size: 20pt; margin: 0 0 2pt; letter-spacing: -0.01em; }
h2 { font-size: 12pt; margin: 18pt 0 6pt; padding-bottom: 3pt; border-bottom: 1.5pt solid var(--ink);
  text-transform: uppercase; letter-spacing: 0.06em; }
h2 { break-after: avoid; }
.items-h { break-before: page; margin-top: 0; }
.sub { color: var(--muted); margin: 0 0 14pt; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8pt 24pt; margin-bottom: 12pt; }
.field { display: flex; gap: 8pt; align-items: baseline; }
.field.wide { grid-column: 1 / -1; }
.label { color: var(--muted); font-size: 9pt; white-space: nowrap; }
.value { flex: 1; border-bottom: 0.75pt solid var(--rule); min-height: 15pt; font-weight: 600; }
.box { background: var(--soft); border: 0.75pt solid var(--rule); border-radius: 4pt; padding: 8pt 10pt; font-size: 9pt; }
.box table { border-collapse: collapse; width: 100%%; }
.box td { padding: 1.5pt 0; vertical-align: top; }
.box td:first-child { color: var(--muted); width: 1.55in; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 8pt; word-break: break-all; }
.statement { font-size: 9.5pt; color: #333; margin: 10pt 0 0; }
table.summary { width: 100%%; border-collapse: collapse; font-size: 9.5pt; }
table.summary th { text-align: left; font-size: 8.5pt; color: var(--muted); font-weight: 600;
  border-bottom: 1pt solid var(--ink); padding: 3pt 6pt 3pt 0; }
table.summary td { border-bottom: 0.5pt solid var(--rule); padding: 3pt 6pt 3pt 0; vertical-align: top; }
table.summary td.n { width: 0.35in; font-weight: 700; }
table.summary td.t { width: 0.9in; white-space: nowrap; color: var(--muted); }
table.summary tr { break-inside: avoid; }
.item { break-inside: avoid; border: 0.75pt solid var(--rule); border-radius: 5pt; margin: 0 0 12pt; overflow: hidden; }
.item-head { display: flex; align-items: baseline; gap: 8pt; padding: 6pt 10pt; background: var(--soft);
  border-bottom: 0.75pt solid var(--rule); }
.num { font-weight: 800; color: #fff; background: var(--accent); border-radius: 3pt; padding: 0 6pt; font-size: 10pt; }
.item-head h3 { margin: 0; font-size: 11.5pt; flex: 1; }
.ts { color: var(--muted); font-size: 8.5pt; white-space: nowrap; }
.item-body { display: flex; gap: 12pt; padding: 10pt; align-items: flex-start; }
.photos { display: flex; flex-wrap: wrap; gap: 6pt; flex: 0 1 auto; max-width: 62%%; }
figure { margin: 0; }
figure img { display: block; max-width: 100%%; max-height: 3.6in; border-radius: 3pt; border: 0.5pt solid var(--rule); }
.photos.multi figure img { max-height: 2.4in; }
figcaption { font-size: 7.5pt; color: var(--muted); margin-top: 2pt; }
.desc { flex: 1; min-width: 0; }
.desc .k { font-size: 8pt; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 2pt; }
.desc p { margin: 0 0 8pt; }
.nophoto { color: var(--muted); font-style: italic; }
.ack { break-inside: avoid; }
.sigs { display: grid; grid-template-columns: 1fr 1fr; gap: 26pt 24pt; margin-top: 14pt; break-inside: avoid; }
.sig { border-top: 0.75pt solid var(--ink); padding-top: 3pt; font-size: 8.5pt; color: var(--muted); margin-top: 28pt; }
.appendix { break-before: page; }
.tr { font-size: 9pt; margin: 0; }
.tr div { display: flex; gap: 8pt; padding: 1.5pt 0; break-inside: avoid; }
.tr span:first-child { color: var(--muted); width: 0.55in; flex: none; font-variant-numeric: tabular-nums; }
"""


def report_image(work: Path, src: Path, max_px: int = 1600) -> Path:
    import cv2
    out = work / "report files" / f"{src.parent.name}_{src.stem}.jpg"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    img = read_img(src)
    h, w = img.shape[:2]
    s = min(1.0, max_px / max(h, w))
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    write_jpg(out, img, 84)
    return out


def build_html(doc: dict, work: Path, paper: str) -> str:
    e = html.escape
    info, v = doc.get("info", {}), doc.get("video", {})
    items = sorted([i for i in doc["items"] if not i.get("exclude")], key=lambda i: i["start"])
    title = info.get("title") or "Pre-Existing Damage Report"
    footer = (title + (" - " + info["property"] if info.get("property") else "")).replace('"', "'")

    def field(label, val, wide=False):
        cls = "field wide" if wide else "field"
        return f'<div class="{cls}"><span class="label">{e(label)}</span><span class="value">{e(val) if val else "&nbsp;"}</span></div>'

    def src(p: Path) -> str:
        return urllib.parse.quote(p.relative_to(work).as_posix())

    size_mb = f"{v['size_bytes'] / 1_048_576:,.1f} MB" if v.get("size_bytes") else ""
    dims = f"{v.get('width')}x{v.get('height')}" + (f" @ {v['fps']:g} fps" if v.get("fps") else "") if v.get("width") else ""
    vrows = [("File", v.get("name", "")), ("Recorded (video metadata)", v.get("recorded", "not stored in file")),
             ("Length", fmt_ts(v["duration"]) if v.get("duration") else ""), ("Size / format", ", ".join(x for x in (size_mb, dims) if x)),
             ("SHA-256 fingerprint", f'<span class="mono">{e(v.get("sha256", ""))}</span>')]
    vtable = "".join(f"<tr><td>{e(k)}</td><td>{val if k.startswith('SHA') else e(str(val))}</td></tr>" for k, val in vrows)

    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{e(title)}</title>",
           f"<meta name='viewport' content='width=device-width, initial-scale=1'><style>{CSS % {'paper': paper, 'footer': e(footer)}}</style></head><body>",
           f"<h1>{e(title)}</h1>",
           f"<p class='sub'>Condition of the property before the tenancy began: {len(items)} item{'s' if len(items) != 1 else ''} of existing damage, recorded on video.</p>",
           "<div class='fields'>",
           field("Property address", info.get("property"), wide=True),
           field("Tenant(s)", info.get("tenant")), field("Landlord / agent", info.get("landlord")),
           field("Inspection date", info.get("inspection_date")), field("Report prepared", dt.date.today().isoformat()),
           "</div>",
           f"<div class='box'><table>{vtable}</table></div>",
           "<p class='statement'>Each photo below is an unedited still frame taken from the video file identified above, at the time shown "
           "under it. Each description is transcribed from the narration recorded in that same video while the damage was being shown. "
           "The SHA-256 fingerprint lets anyone confirm they are looking at the same, unaltered video file.</p>"]
    if info.get("notes"):
        out.append(f"<p class='statement'><b>Notes:</b> {e(info['notes'])}</p>")

    out.append("<h2>Summary</h2><table class='summary'><tr><th>#</th><th>Location</th><th>Damage</th><th>Video time</th></tr>")
    for n, it in enumerate(items, 1):
        d = it.get("summary") or it.get("description", "")
        d = d if len(d) <= 140 else d[:137].rsplit(" ", 1)[0] + "..."
        out.append(f"<tr><td class='n'>{n}</td><td>{e(it.get('location') or '-')}</td><td>{e(d)}</td>"
                   f"<td class='t'>{fmt_ts(it['start'])} - {fmt_ts(it['end'])}</td></tr>")
    out.append("</table><h2 class='items-h'>Items</h2>")

    for n, it in enumerate(items, 1):
        cmap = {c["id"]: c for c in it.get("candidates", [])}
        figs = []
        for pid in it.get("photos", []):
            c = cmap.get(pid)
            if not c or not (work / c["file"]).exists():
                print(f"  warning: item {it['id']} photo '{pid}' not found - skipped")
                continue
            img = report_image(work, work / c["file"])
            figs.append(f"<figure><img src='{src(img)}' alt='Item {n} photo'><figcaption>Video frame at {fmt_ts(c['t'], True)}</figcaption></figure>")
        photos = f"<div class='photos{' multi' if len(figs) > 1 else ''}'>{''.join(figs)}</div>" if figs else ""
        paras = "".join(f"<p>{e(p.strip())}</p>" for p in re.split(r"\n\s*\n", it.get("description", "")) if p.strip())
        out.append(
            f"<section class='item'><div class='item-head'><span class='num'>{n}</span>"
            f"<h3>{e(it.get('location') or 'Location not stated')}</h3>"
            f"<span class='ts'>Video {fmt_ts(it['start'])} - {fmt_ts(it['end'])}</span></div>"
            f"<div class='item-body'>{photos or '<p class=nophoto>No still selected.</p>'}"
            f"<div class='desc'><p class='k'>Description</p>{paras}</div></div></section>")

    out.append("<section class='ack'><h2>Acknowledgement</h2><p class='statement'>The parties below agree that this report records "
               "damage that existed at the start of the tenancy.</p><div class='sigs'>"
               "<div class='sig'>Tenant signature</div><div class='sig'>Date</div>"
               "<div class='sig'>Landlord / agent signature</div><div class='sig'>Date</div></div></section>")

    tpath = work / TRANSCRIPT
    if tpath.exists():
        segs = json.loads(tpath.read_text(encoding="utf-8")).get("segments", [])
        out.append("<section class='appendix'><h2>Appendix: full narration transcript</h2>"
                   "<p class='statement'>Machine transcription of the whole video's audio, with times, for cross-checking.</p><div class='tr'>")
        out += [f"<div><span>{fmt_ts(a)}</span><span>{e(text)}</span></div>" for a, _, text in sentences(segs)]
        out.append("</div></section>")
    out.append("</body></html>")
    return "\n".join(out)


def find_browser() -> Path | None:
    """Edge or Chrome, used headless to print the HTML report to PDF."""
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = Path(root) / rel
            if p.exists():
                return p
    for mac in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if Path(mac).exists():
            return Path(mac)
    for name in ("msedge", "microsoft-edge", "google-chrome", "chrome", "chromium", "chromium-browser"):
        w = shutil.which(name)
        if w:
            return Path(w)
    return None


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    browser = find_browser()
    if not browser:
        return False
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
        cmd = [str(browser), "--headless", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
               "--print-to-pdf-no-header", f"--user-data-dir={prof}", f"--print-to-pdf={pdf_path}", html_path.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=240)
        except subprocess.TimeoutExpired:
            return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def cmd_report(args, work: Path | None = None) -> None:
    work = work or Path(args.workdir).resolve()
    doc = load_doc(work)
    changed = False
    for k in ("property", "tenant", "landlord", "inspection_date"):
        val = getattr(args, k, None)
        if val:
            doc.setdefault("info", {})[k] = val
            changed = True
    if changed:
        save_doc(work, doc)
    paper = args.paper or default_paper()
    html_path = work / f"{REPORT_NAME}.html"
    html_path.write_text(build_html(doc, work, paper), encoding="utf-8")
    pdf_path = work / f"{REPORT_NAME}.pdf"
    try:
        pdf_path.unlink(missing_ok=True)
    except PermissionError:
        sys.exit(f"Close '{pdf_path.name}' (it is open in another program) and run again.")
    if html_to_pdf(html_path, pdf_path):
        print(f"Report ready:\n  {pdf_path}\n  {html_path}")
    else:
        print(f"HTML report ready: {html_path}\nCould not auto-create the PDF - open the HTML in Edge/Chrome and Print > Save as PDF.")


def cmd_all(args) -> None:
    work = cmd_transcribe(args)
    cmd_frames(args, work)
    cmd_report(args, work)
    print("\nCheck the PDF. To fix wording, locations, or photos, edit items.json (or see frames/item_NN/contact.jpg "
          "and set \"photos\") and run the 'report' step again.")


# --------------------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_transcribe_opts(sp):
        sp.add_argument("video")
        sp.add_argument("--model", default="medium.en", help="Whisper model: small.en (fast), medium.en (default), large-v3 (best, slow)")
        sp.add_argument("--language", default="en")
        sp.add_argument("--gap", type=float, default=2.0, help="seconds of silence that start a new item in the draft (default 2.0)")
        sp.add_argument("--force", action="store_true", help="redo the transcript and overwrite items.json")

    def add_frames_opts(sp):
        sp.add_argument("--candidates", type=int, default=8, help="candidate stills per item (default 8)")
        sp.add_argument("--sample-fps", type=float, default=8.0, help="frames checked per second of video (default 8)")
        sp.add_argument("--pre", type=float, default=0.5, help="seconds before you start talking to consider")
        sp.add_argument("--post", type=float, default=1.5, help="seconds after you stop talking to consider")
        sp.add_argument("--redo", action="store_true", help="redo stills for every item (resets photo picks)")
        sp.add_argument("--only", help="comma-separated item ids to redo, e.g. 3,7")

    def add_report_opts(sp):
        sp.add_argument("--property", help="property address")
        sp.add_argument("--tenant")
        sp.add_argument("--landlord")
        sp.add_argument("--inspection-date", dest="inspection_date")
        sp.add_argument("--paper", choices=["Letter", "A4"], help="paper size (default from Windows region)")

    sp = sub.add_parser("all", help="transcribe + frames + report")
    add_transcribe_opts(sp); add_frames_opts(sp); add_report_opts(sp)
    sp = sub.add_parser("transcribe", help="transcribe audio and draft items.json")
    add_transcribe_opts(sp)
    sp = sub.add_parser("frames", help="extract candidate stills per item")
    sp.add_argument("workdir"); add_frames_opts(sp)
    sp = sub.add_parser("overview", help="contact sheet of one frame every N seconds across the whole video")
    sp.add_argument("workdir"); sp.add_argument("--every", type=float, default=1.0, help="seconds between frames (default 1)")
    sp = sub.add_parser("grab", help="pull a still at a given time for an item")
    sp.add_argument("workdir"); sp.add_argument("--item", type=int, required=True); sp.add_argument("time")
    sp.add_argument("--snap", type=float, default=0.4, help="pick the sharpest frame within +/- this many seconds")
    sp.add_argument("--add", action="store_true", help="add as an extra photo instead of replacing")
    sp = sub.add_parser("report", help="build Damage Report.html/.pdf")
    sp.add_argument("workdir"); add_report_opts(sp)

    args = p.parse_args()
    {"all": cmd_all, "transcribe": cmd_transcribe, "frames": cmd_frames, "overview": cmd_overview,
     "grab": cmd_grab, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
