#!/usr/bin/env python3
"""Draw one SVG illustration per word: up to three panels, one per card field it helps.

    ./draw_illustrations.py [--ids a,b] [--redo] [--jobs 4] [--model M] [--dry]

Every word the sheet carries (the ones with a Deep dive) gets a picture. It is one SVG
with up to three panels, stacked in card order and labelled with the field each one
illustrates, so the eye can tie a panel to the text next to it:

    MEANING    the word's core image: what it borrows, a literal scene, a shape
    EXAMPLE    the situation of the sentence he actually met the word in
    DEEP DIVE  the near-synonyms from the comparison lines, drawn as a minimal pair

Not every word needs all three. The model draws the fewest panels that carry the word and
names them in the SVG's <title>, which is logged and then stripped. A brief can steer it:
the answer's `pic:` line (stored as `pic` on the history item), or a line in `pics.jsonl`,
which wins — `pic: example+deep | notes`, or `pic: none` for no picture at all. With no
brief the model decides alone, which is how words captured before `pic:` existed get one.

What comes back is not trusted: `validate()` is the gate between a model's output and a
card that runs inside Anki's webview, and anything it refuses never reaches the sheet.
One retry is allowed, with the refusal reasons handed back.

The result is `illustrations/<id>.svg`, one line of markup, and the sheet is flushed
again so the picture lands in the `Illustration` column. An existing file is never
redrawn unless asked (`--redo`), and a file whose root carries `data-hand` is never
redrawn at all — that is a picture Tân drew himself, and it outranks any model's.

The Stop hook starts this in the background after every flush that brought in a new
word, so nobody has to run it by hand; running it by hand is for redraws and backfills.
Every call is logged to `illustrations/draw-log.jsonl` with its cost as the CLI reports it.
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

from flush_queue import HISTORY, ILLUSTRATIONS as OUT_DIR, deep_parts, vocab_row

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
MAX_BYTES = 10240       # a chosen ceiling, not a measurement: one panel came out at
                        # 1.7–3 KB in the trial, and much past three of those is clutter
MAX_TEXT_WORDS = 36     # node letters (A, B, C) do not count as words
PANELS = ("meaning", "example", "deep")

# Measured on the same 6 words, pictures compared side by side: Sonnet passed the
# validator as often as Opus, drew them as clearly, and cost $0.038 a word against Opus's
# $0.062. `--model opus` is still there for a word Sonnet draws badly.
DEFAULT_MODEL = "sonnet"

SYSTEM = ("You draw small flashcard illustrations as a single inline SVG. "
          "Reply with the SVG markup only: no prose, no code fence.")

PROMPT = """Draw ONE flashcard illustration for an English learner who is a software developer. It sits on the back of the card, right under the meaning.

The card:
Word: {word} ({pos})
Meaning: {meaning}
Example (the sentence the learner actually met the word in): {example}
Deep dive (Vietnamese notes on how it differs from its near-synonyms): {deep}
Comparison lines (one sentence per near-synonym, same scenario, the word itself last):
{contrasts}

Brief from the tutor (may be empty; then decide yourself): {brief}

The picture has up to three panels, one per card field it illustrates:
- MEANING: the word's core image: the physical thing it borrows (bottleneck = a narrow neck), the literal scene of a phrasal verb, or the shape of the concept (a chain, a loop).
- EXAMPLE: the concrete situation of the example sentence, using its own nouns and labels (the service, the pod, the doc…), so the learner recognises the moment he met the word.
- DEEP DIVE: the near-synonyms from the comparison lines as a minimal pair: the same small scene once per word, only each word's effect changes, each part labelled with its word, "{word}" last and in #f08c00.

Choose the panels. Use the fewest that carry the word:
- Skip MEANING when the EXAMPLE scene already shows the meaning.
- Skip EXAMPLE when the sentence is abstract and a scene adds nothing, or when MEANING already is that scene.
- Skip DEEP DIVE when there are no comparison lines or the difference cannot be drawn.
One panel is fine. Three only when each shows something the other two do not. Never draw the word's topic as an icon: that teaches nothing.

Layout:
- Panels stacked top to bottom in the order MEANING, EXAMPLE, DEEP DIVE, separated by a thin horizontal line (currentColor, stroke-opacity .2).
- Each panel begins with its label at its top-left, exactly MEANING, EXAMPLE or DEEP DIVE: font-size 9, letter-spacing 1, fill currentColor, fill-opacity .55.
- Each panel is 110 to 160 units tall.
- The first child of <svg> is <title>panels: …</title> naming the panels you drew in order, lowercase, joined by + (for example <title>panels: example+deep</title>).

Hard rules (the SVG is rejected otherwise):
- Root: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 H" width="100%" style="max-width:340px" font-family="inherit">, H = the sum of the panel heights.
- Elements allowed: g, path, rect, circle, ellipse, line, polyline, polygon, text, tspan, title. Nothing else: no style, defs, marker, image, use, script, foreignObject, a.
- No id, class, href or on* attributes.
- Colours: ink is currentColor (so it works on white AND dark cards); the only other colours are #f08c00 #339af0 #37b24d #fa5252, and #fff only for text on a filled accent shape. Use fill-opacity/stroke-opacity for faint parts.
- Arrowheads are small filled paths/polygons, not markers.
- Text: English only, about 30 words in total not counting the panel labels (single node letters like A, B, C do not count either), font-size 10 to 15. Keep every label fully inside the viewBox: budget about 0.6 × font-size per character, and never let two labels overlap each other or a shape.
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
    """id -> (history item, brief) for every word the sheet carries.

    Same choice as the flush: the latest capture that has a Deep dive. The brief is
    `pics.jsonl` first, then the item's own `pic`, else empty for the model to decide;
    `none` from either means no picture.
    """
    items = {}
    for item in read_jsonl(HISTORY):
        if item.get("kind", "vocab") != "vocab" or not deep_parts(item)[0]:
            continue
        try:
            items[vocab_row(item)["ID"]] = item
        except KeyError:
            continue
    briefs = {i: item.get("pic", "") for i, item in items.items()}
    for row in read_jsonl(PICS):
        if row["id"] in items:
            briefs[row["id"]] = row["pic"]
    return {i: (items[i], (b or "").strip()) for i, b in briefs.items()
            if (b or "").strip().lower() != "none"}


def extract_svg(text):
    start, end = text.find("<svg"), text.rfind("</svg>")
    return text[start:end + len("</svg>")] if start != -1 and end != -1 else ""


def _local(tag):
    return tag.split("}", 1)[-1]


def validate(svg):
    """Returns (clean one-line svg, [], panels) or (None, [reasons], "").

    Safety first (tags, attributes, links), then the two things that keep a picture
    usable on a card: colours that survive night mode, and a size that means simple.
    """
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return None, [f"not well-formed XML: {exc}"], ""
    problems = []
    if _local(root.tag) != "svg":
        problems.append("root element is not <svg>")
    if not root.get("viewBox"):
        problems.append("root <svg> has no viewBox")
    panels = ""
    title = root.find(f"{{{SVG_NS}}}title")
    if title is not None and (title.text or "").strip().lower().startswith("panels:"):
        panels = title.text.split(":", 1)[1].strip().lower().replace(" ", "")
    chosen = panels.split("+") if panels else []
    if not chosen or any(p not in PANELS for p in chosen):
        problems.append("first child must be <title>panels: …</title> using meaning, example, deep joined by +")
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
        problems.append(f"{words} words of text; keep it to about 30 plus the panel labels")
    if problems:
        return None, sorted(set(problems)), ""

    # The <title> was for the log; on a card it would only surface as a hover tooltip.
    root.remove(title)

    # Normalise the root so every picture sits on the card the same way: full width up
    # to 340px, height from the viewBox's aspect ratio rather than a fixed number.
    root.attrib.pop("height", None)
    root.set("width", "100%")
    if "max-width" not in root.get("style", ""):
        root.set("style", "max-width:340px")
    root.set("font-family", "inherit")
    # Whitespace runs become one space, never nothing: inside <text>, the space between
    # "step player =" and a <tspan> is part of the sentence. Stripping it glued words.
    for el in root.iter():
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)
        if el.tail:
            el.tail = re.sub(r"\s+", " ", el.tail)
    out = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    out = out.replace("\n", " ").replace("\t", " ")
    if len(out.encode("utf-8")) > MAX_BYTES:
        return None, [f"{len(out.encode('utf-8'))} bytes, over the {MAX_BYTES} ceiling: simplify"], ""
    return out, [], panels


def ask(prompt, model=None):
    """One isolated headless call: no settings (so no hooks, no CLAUDE.md), no tools,
    no MCP, run from an empty directory so no project instructions are found either."""
    cmd = ["claude", "-p", "--setting-sources", "", "--tools", "", "--strict-mcp-config",
           "--output-format", "json", "--system-prompt", SYSTEM]
    cmd += ["--model", model or DEFAULT_MODEL]
    with tempfile.TemporaryDirectory() as empty:
        proc = subprocess.run(cmd + [prompt], cwd=empty, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    data = json.loads(proc.stdout)
    usage = data.get("modelUsage") or {}
    model, u = next(iter(usage.items()), (None, {}))
    return data.get("result", ""), {
        "cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms"),
        "model": model,
        # Cached input is still input: the three counts together are what was read.
        "input_tokens": sum(u.get(k, 0) for k in
                            ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens")),
        "output_tokens": u.get("outputTokens"),
        "thinking_tokens": u.get("thinkingTokens"),
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
    prose, contrasts = deep_parts(item)
    prompt = PROMPT.format(
        word=item["word"], pos=item.get("pos", ""), meaning=item.get("meaning", ""),
        example=item.get("example", ""), deep=prose,
        contrasts="\n".join(f"  · {c}" for c in contrasts) or "  (none)",
        brief=spec, max_kb=MAX_BYTES // 1024)
    for attempt in (1, 2):
        try:
            reply, meta = ask(prompt, model)
        except Exception as exc:     # a failed call is logged and skipped, never fatal
            log({"id": word_id, "attempt": attempt, "ok": False, "problems": [str(exc)[:300]]})
            return word_id, False, [str(exc)[:300]]
        raw = extract_svg(reply)
        svg, problems, panels = validate(raw) if raw else (None, ["no <svg> in the reply"], "")
        log({"id": word_id, "attempt": attempt, "ok": svg is not None, "problems": problems,
             "panels": panels, "bytes": len(svg.encode()) if svg else None, "spec": spec, **meta})
        if svg:
            with open(os.path.join(OUT_DIR, f"{word_id}.svg"), "w", encoding="utf-8") as fh:
                fh.write(svg + "\n")
            return word_id, True, []
        prompt = RETRY.format(problems="\n".join(f"- {p}" for p in problems), svg=raw or reply[:4000])
    return word_id, False, problems


def say(message):
    """Timestamped, because the hook appends this output to capture.log."""
    print(f"{datetime.now().isoformat(timespec='seconds')} draw: {message}", flush=True)


def pending(ids=None, redo=False, skip=(), new_only=False):
    specs = collect_specs()
    if ids:
        specs = {k: v for k, v in specs.items() if k in ids}
    if new_only:
        # Captured by the current hook, which always writes `contrasts`. Older words are
        # a backfill, and a backfill costs money: it runs only when started by hand.
        specs = {k: v for k, v in specs.items() if "contrasts" in v[0]}
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
    ap.add_argument("--model", help=f"passed to claude --model; default: {DEFAULT_MODEL}")
    ap.add_argument("--dry", action="store_true", help="list what would be drawn")
    ap.add_argument("--new", action="store_true",
                    help="only words captured by the current hook (what the hook runs); "
                         "without it every word missing a picture is drawn")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ids = {i.strip() for i in args.ids.split(",")} if args.ids else None
    if args.dry:
        todo = pending(ids, args.redo, new_only=args.new)
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
        todo = pending(ids, redo, skip=failed, new_only=args.new)
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
