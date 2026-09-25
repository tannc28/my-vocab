#!/usr/bin/env python3
"""Draw one SVG illustration per word, from a one-line `pic:` spec.

    ./draw_illustrations.py [--ids a,b] [--redo] [--jobs 4] [--model M] [--dry]

The Stop hook starts it in the background after every flush that brought in a word
with a `pic:` line, so nobody has to run it by hand; running it by hand is for redraws.

A spec says what the picture is for, not how to draw it:

    contrast | revert · roll back · back out | one commit line A–B–C, only the action changes

(type | subjects | scene). Specs come from two places: a `pic` field on a history item,
which is where the Stop hook will put them once the answer carries a `pic:` line, and
`pics.jsonl`, written by hand, which wins — it is how old words get a picture without
rewriting history, and how a spec gets corrected.

Each spec goes to a headless `claude -p` with the word's own meaning, example and Deep
dive, so the picture is drawn from the same context the card teaches. What comes back
is not trusted: `validate()` is the gate between a model's output and a card that runs
inside Anki's webview, and anything it refuses never reaches the sheet. One retry is
allowed, with the refusal reasons handed back.

The result is `illustrations/<id>.svg`, one line of markup, and the sheet is flushed
again so the picture lands in the `Illustration` column. An existing file is never
redrawn unless asked (`--redo`), and a file whose root carries `data-hand` is never
redrawn at all — that is a picture Tân drew himself, and it outranks any model's.

Every call is logged to `illustrations/draw-log.jsonl` with its cost as the CLI reports
it, because what a picture costs is one of the things this trial exists to measure.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flush_queue import HISTORY, ILLUSTRATIONS as OUT_DIR, vocab_row

VOCAB_DIR = os.path.dirname(os.path.abspath(__file__))
PICS = os.path.join(VOCAB_DIR, "pics.jsonl")
LOG = os.path.join(OUT_DIR, "draw-log.jsonl")
LOCK = os.path.join(OUT_DIR, ".draw.lock")
FLUSH = os.path.join(VOCAB_DIR, "flush_queue.py")

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)   # otherwise every tag is written back as ns0:svg

# Ink is currentColor so the picture follows the card's text colour into night mode.
# The four accents are mid-tones that stay readable on both a white and a dark card;
# white is only for a label sitting on a filled accent shape.
PALETTE = {"currentcolor", "none", "#f08c00", "#339af0", "#37b24d", "#fa5252",
           "#fff", "#ffffff", "white"}

# Shapes and text, nothing else. No <defs>/<marker>: they need ids, and ids from two
# pictures on one card would collide. No <style>: inside an HTML page an inline SVG's
# stylesheet applies to the whole document, so it would restyle the card around it.
ALLOWED_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
                "polygon", "text", "tspan", "title"}
COLOUR_ATTRS = {"fill", "stroke", "color"}
MAX_BYTES = 6144        # a chosen ceiling, not a measurement: the hand-drawn samples
                        # are 1–2.3 KB, and much bigger usually means a cluttered picture
MAX_TEXT_WORDS = 20      # node letters (A, B, C) do not count as words

SYSTEM = ("You draw small flashcard illustrations as a single inline SVG. "
          "Reply with the SVG markup only: no prose, no code fence.")

PROMPT = """Draw ONE flashcard illustration for an English learner who is a software developer.

Word: {word} ({pos})
Meaning: {meaning}
The sentence the learner met it in: {example}
How it differs from its near-synonyms (Vietnamese notes): {deep}

Picture type: {type}
Subjects: {subjects}
Scene: {scene}

What the picture must do:
- Show what makes "{word}" DIFFERENT from its near-synonyms, not just its topic. An icon of the topic is a failure.
- type=contrast: the same scene once per subject, stacked or side by side; only each subject's effect changes. Label each part with its subject; "{word}" is the one in #f08c00.
- type=literal: left half the literal, physical meaning; right half the developer meaning; a small arrow between.
- type=metaphor: the physical image the word borrows, with the developer meaning visible in it.
- type=structure: the structure itself (chain, loop, tree), with the one property that matters made obvious.
- type=idiom: the scene the idiom paints, simple and a little funny.

Hard rules (the SVG is rejected otherwise):
- Root: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 H" width="100%" style="max-width:340px" font-family="inherit">, H between 140 and 230.
- Elements allowed: g, path, rect, circle, ellipse, line, polyline, polygon, text, tspan, title. Nothing else: no style, defs, marker, image, use, script, foreignObject, a.
- No id, class, href or on* attributes.
- Colours: ink is currentColor (so it works on white AND dark cards); the only other colours are #f08c00 #339af0 #37b24d #fa5252, and #fff only for text on a filled accent shape. Use fill-opacity/stroke-opacity for faint parts.
- Arrowheads are small filled paths/polygons, not markers.
- Text: English only, about 14 words in total (single node letters like A, B, C do not count), font-size 10 to 15. Keep every label fully inside the viewBox: budget about 0.6 × font-size per character, and never let two labels overlap each other or a shape.
- Under {max_kb} KB. Simple, flat, clear at 340px wide.
"""

RETRY = """Your SVG was rejected for these reasons:
{problems}

Here it is again. Fix exactly those problems, keep the picture otherwise the same, and reply with the SVG only.

{svg}"""


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def collect_specs():
    """id -> (history item, spec). The latest capture of a word is the one the card shows."""
    items = {}
    for item in read_jsonl(HISTORY):
        if item.get("kind", "vocab") != "vocab":
            continue
        try:
            items[vocab_row(item)["ID"]] = item
        except KeyError:
            continue
    specs = {i: item["pic"] for i, item in items.items() if item.get("pic")}
    for row in read_jsonl(PICS):
        specs[row["id"]] = row["pic"]
    out = {}
    for word_id, spec in specs.items():
        if word_id not in items:
            print(f"skip {word_id}: no such word in history", file=sys.stderr)
            continue
        if spec.strip().lower() == "none":
            continue
        out[word_id] = (items[word_id], spec)
    return out


def parse_spec(spec):
    parts = [p.strip() for p in spec.split("|")]
    parts += [""] * (3 - len(parts))
    return {"type": parts[0], "subjects": parts[1], "scene": " | ".join(parts[2:]).strip(" |")}


def extract_svg(text):
    start, end = text.find("<svg"), text.rfind("</svg>")
    return text[start:end + len("</svg>")] if start != -1 and end != -1 else ""


def _local(tag):
    return tag.split("}", 1)[-1]


def validate(svg):
    """Returns (clean one-line svg, []) or (None, [reasons]).

    Safety first (tags, attributes, links), then the two things that keep a picture
    usable on a card: colours that survive night mode, and a size that means simple.
    """
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return None, [f"not well-formed XML: {exc}"]
    problems = []
    if _local(root.tag) != "svg":
        problems.append("root element is not <svg>")
    if not root.get("viewBox"):
        problems.append("root <svg> has no viewBox")
    words = 0
    for el in root.iter():
        tag = _local(el.tag)
        if tag not in ALLOWED_TAGS:
            problems.append(f"<{tag}> is not allowed")
        for name, value in el.attrib.items():
            attr = _local(name).lower()
            if attr in ("id", "class") or attr.startswith("on") or attr == "href":
                problems.append(f'attribute {attr}="{value[:30]}" on <{tag}> is not allowed')
            if attr in COLOUR_ATTRS and value.strip().lower() not in PALETTE:
                problems.append(f'colour {attr}="{value}" on <{tag}> is outside the palette')
            if attr == "style" and re.search(r"url\(|@|expression|(?<![-\w])(fill|stroke|color)\s*:", value, re.I):
                problems.append(f'style="{value}" may only size the picture, not colour or link it')
        if tag in ("text", "tspan") and el.text:
            words += sum(1 for w in el.text.split() if len(re.sub(r"\W", "", w)) > 1)
    if words > MAX_TEXT_WORDS:
        problems.append(f"{words} words of text; keep it to about 14")
    if problems:
        return None, sorted(set(problems))

    # Normalise the root so every picture sits on the card the same way: full width up
    # to 340px, height from the viewBox's aspect ratio rather than a fixed number.
    root.attrib.pop("height", None)
    root.set("width", "100%")
    if "max-width" not in root.get("style", ""):
        root.set("style", "max-width:340px")
    root.set("font-family", "inherit")
    for el in root.iter():
        if el.text:
            el.text = " ".join(el.text.split())
        if el.tail:
            el.tail = el.tail.strip() or None
    out = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    out = re.sub(r">\s+<", "><", out).replace("\n", " ").replace("\t", " ")
    if len(out.encode("utf-8")) > MAX_BYTES:
        return None, [f"{len(out.encode('utf-8'))} bytes, over the {MAX_BYTES} ceiling: simplify"]
    return out, []


def ask(prompt, model=None):
    """One isolated headless call: no settings (so no hooks, no CLAUDE.md), no tools,
    no MCP, run from an empty directory so no project instructions are found either."""
    cmd = ["claude", "-p", "--setting-sources", "", "--tools", "", "--strict-mcp-config",
           "--output-format", "json", "--system-prompt", SYSTEM]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory() as empty:
        proc = subprocess.run(cmd + [prompt], cwd=empty, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    data = json.loads(proc.stdout)
    models = list((data.get("modelUsage") or {}).keys())
    return data.get("result", ""), {
        "cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms"),
        "model": models[0] if models else None,
    }


def log(entry):
    entry["at"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def is_hand_drawn(path):
    with open(path, encoding="utf-8") as fh:
        head = fh.read(400)
    return "data-hand" in head.split(">", 1)[0]


def draw(word_id, item, spec, model=None):
    s = parse_spec(spec)
    prompt = PROMPT.format(
        word=item["word"], pos=item.get("pos", ""), meaning=item.get("meaning", ""),
        example=item.get("example", ""), deep=item.get("deep", ""),
        max_kb=MAX_BYTES // 1024, **s)
    for attempt in (1, 2):
        try:
            reply, meta = ask(prompt, model)
        except Exception as exc:     # a failed call is logged and skipped, never fatal
            log({"id": word_id, "attempt": attempt, "ok": False, "problems": [str(exc)[:300]]})
            return word_id, False, [str(exc)[:300]]
        raw = extract_svg(reply)
        svg, problems = validate(raw) if raw else (None, ["no <svg> in the reply"])
        log({"id": word_id, "attempt": attempt, "ok": svg is not None, "problems": problems,
             "bytes": len(svg.encode()) if svg else None, "spec": spec, **meta})
        if svg:
            with open(os.path.join(OUT_DIR, f"{word_id}.svg"), "w", encoding="utf-8") as fh:
                fh.write(svg + "\n")
            return word_id, True, []
        prompt = RETRY.format(problems="\n".join(f"- {p}" for p in problems), svg=raw or reply[:4000])
    return word_id, False, problems


def say(message):
    """Timestamped, because the hook appends this output to capture.log."""
    print(f"{datetime.now().isoformat(timespec='seconds')} draw: {message}", flush=True)


def pending(ids=None, redo=False, skip=()):
    specs = collect_specs()
    if ids:
        specs = {k: v for k, v in specs.items() if k in ids}
    todo = []
    for word_id, (item, spec) in specs.items():
        if word_id in skip:
            continue
        path = os.path.join(OUT_DIR, f"{word_id}.svg")
        if os.path.exists(path) and (not redo or is_hand_drawn(path)):
            continue
        todo.append((word_id, item, spec))
    return todo


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ids", help="comma-separated word ids; default: every word with a spec")
    ap.add_argument("--redo", action="store_true", help="redraw even if a file exists (never a data-hand one)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--model", help="passed to claude --model; default: the CLI's default")
    ap.add_argument("--dry", action="store_true", help="list what would be drawn")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ids = {i.strip() for i in args.ids.split(",")} if args.ids else None
    if args.dry:
        todo = pending(ids, args.redo)
        print(f"{len(todo)} to draw" + (f": {', '.join(t[0] for t in todo)}" if todo else ""))
        return

    # One drawer at a time. Every Stop can start one, and a turn can end while the last
    # one is still waiting on the model; the second simply leaves, because the first
    # looks for new specs again before it exits and will pick those words up.
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("another drawer is running; it will pick these up")
        return

    redo, drawn, failed = args.redo, 0, set()
    while True:
        todo = pending(ids, redo, skip=failed)
        redo = False             # --redo applies to the first pass only, or it never ends
        if not todo:
            break
        say(f"{len(todo)} to draw: {', '.join(t[0] for t in todo)}")
        started = time.time()
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for word_id, ok, problems in pool.map(lambda t: draw(*t, model=args.model), todo):
                say(("ok   " if ok else "FAIL ") + word_id + ("" if ok else f": {'; '.join(problems)}"))
                drawn += ok
                if not ok:
                    failed.add(word_id)
        # A spec that failed twice will fail again on the next loop; it is skipped for
        # the rest of this run and left for a later one (or a better spec) instead of
        # spending money in a circle.
        say(f"pass done in {time.time() - started:.0f}s")

    if drawn:
        done = subprocess.run([sys.executable, FLUSH], capture_output=True, text=True, timeout=120)
        say(f"flush rc={done.returncode} {done.stdout.strip()}{done.stderr.strip()}")


if __name__ == "__main__":
    main()
