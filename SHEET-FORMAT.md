# my-vocab sheet — format an agent must follow

Hand this file to an agent and it has everything it needs to add vocabulary rows.
The deck is built by **[sheets2anki](https://github.com/tannc28/sheets2anki)**, so this
file describes *its* contract, not a private one.

- Sheet: `~/TanCode/NOTE/vocab/my-vocab-sheet.xlsx`, tab `vocab`
- Bootstrap an empty one: `python3 gen_sheet.py my-vocab-sheet.xlsx` (overwrites — never
  run it on a sheet that already holds words)
- Dictionary lookups: `python3 ~/.claude/skills/my-vocab/lookup.py lookup <word>...`

---

## 1. How sheets2anki reads the sheet

Four rules, and everything else follows from them.

1. **Row 1 is the header.** Only four names are reserved: `ID`, `SYNC`, `SUBDECK n`,
   `TAGS`. Matching ignores case and surrounding spaces.
2. **Every other column becomes an Anki field named after its header.** There is no fixed
   column list — the card design *is* the column list.
3. **Left-to-right order is card order, and the first content column is the front.**
   `side=front` in the config row moves more columns to the front.
4. **Row 2 is the config row** because its `ID` cell starts with `#config`. Data starts at
   row 3. Each cell holds `key=value; key=value` for *that column*; an empty cell means
   defaults.

The add-on then fetches the sheet as TSV, so a cell may contain a newline (it gets quoted)
but **must not contain a tab**.

## 2. The columns

| # | Header | Role |
|---|---|---|
| A | `ID` | permanent key, `<word>-<pos>` |
| B | `SYNC` | always `TRUE` |
| C | `SUBDECK 1` | the day, `YYYY-MM-DD` |
| D | `Word` | front |
| E | `IPA` | front |
| F | `POS` | front |
| G | `Meaning` | back — English-to-English |
| H | `Collocation` | back |
| I | `Example` | back |

Config row (row 2) — do not rewrite it when adding words:

```
ID           #config align=center
Word         side=front; size=40; bold; tts=en_US
IPA          side=front; size=18; color=muted
POS          side=front; size=14; color=accent; italic
Meaning      size=22
Collocation  label=Collocations; size=17; color=accent
Example      label=Examples; size=17; italic; tts=en_US
```

Which produces one card:

- **Front** — the word at 40 px, read aloud by the system voice, then IPA and part of speech.
- **Back** — meaning, collocations, then all examples, also read aloud.

## 3. Rules for writing a row

**`ID` — `<word>-<pos>`, lower-case, non-word characters replaced by `-`.**
Example: `work-out-phrasal-verb`. Uniqueness is *(word, pos)* and nothing else: one row per
word, for good.

**A word met again does not make a second row — it moves to the day it was met again.**
The later capture replaces the row wholesale, `Example` included, and the row leaves its
old day for today's, at the top of the sheet. A word that keeps coming back is the one
worth seeing again, and reading today's rows is how that gets noticed; leaving it parked
under the day it was first met hides it, and stacking sentences onto it makes the row
harder to read every time.

**A word in `known-words.txt` never becomes a row at all.** That file is Tân's own list of
words he already knows, one per line, matched on the whole line lower-cased. It is read
twice: a SessionStart hook shows it to the answer so the word is not written, and
`vocab-capture.py` checks it again before queueing, which is the half that holds when the
first is forgotten. Adding a word there does not delete the row it already has — the sheet
is rebuilt from `history.jsonl`, and a card already in Anki carries review history worth
more than the tidiness of removing it.

Never edit an `ID` afterwards: it is how Anki matches a note back to its row, and changing
it strands the review history.

**Insert new rows at row 3**, directly under the config row, so the newest words are on top.

**`SUBDECK 1` is the day the word was added**, which puts it in `…::2026-08-11`. That is
where the per-day structure comes from — one sheet, one link, one deck tree.

**`Example` holds 2–3 sentences, joined by `<br>`** — `<br>` because Anki renders the field
as HTML and a bare newline would collapse to a space.

### What goes in each cell

- **`IPA` — write it when you know it, look it up when you don't.** `lookup.py lookup <word>`
  is the fallback, not a step to run every time: calling it for a word you can already
  transcribe is a round-trip that buys nothing.
  **What counts as knowing it**: the whole transcription, stress mark included, in General
  American — not a rough shape of the word. Doubt about which syllable carries the stress, an
  unstressed vowel that could be `/ə/` or `/ɪ/`, or a word whose US and UK forms diverge is
  not knowing it; that is the case the fallback exists for, and it is exactly the case a B2+
  word tends to be.
  From `lookup.py`, copy `pronunciations[].text` **verbatim** and prefer the entry tagged
  `variety: "us"` — that tag comes from the audio filename and is trustworthy. If it returns
  no transcription either, leave the cell empty. An invented IPA is worse than a missing one:
  a missing one teaches nothing, a wrong one teaches a wrong pronunciation and gets rehearsed
  until it sticks.
- **`POS` + `Meaning` — pick the sense that matches the context.** `lookup.py` returns senses
  unfiltered and the first is often the wrong one (`bloat` starts at the veterinary sense).
  Read them all, choose the one actually met, and copy that `definition` verbatim. If no
  returned sense fits — common for a software meaning a general dictionary lacks — write it
  yourself in B1–B2 English.
- **`Collocation` — 2–3 real ones**, joined by ` · `, the kind that make the word sound native
  (`throttle the request rate`, not `do a throttle`). Prefer developer English when the word
  lives there.
- **`Example` — the first sentence is the one Tân actually met the word in.** That is what
  anchors the word to a real memory, so it outranks anything the dictionary offers. Then the
  dictionary's own example when the chosen sense has one, then one more showing a different
  collocation or grammatical pattern. Vary the structure across them.

## 4. The `grammar` tab

A second tab in the same file, same four rules, different subject: the corrections made to
Tân's own English. sheets2anki is pointed at one tab at a time (the `gid` in the URL), so
this is a second deck, not a change to the vocab one.

| # | Header | Role |
|---|---|---|
| A | `ID` | `g-<sha1[:8] of Original>` |
| B | `SYNC` | always `TRUE` |
| C | `SUBDECK 1` | the day he was corrected, `YYYY-MM-DD` |
| D | `Original` | front — the sentence exactly as he wrote it |
| E | `Corrected` | back — the `Q:` rewrite |
| F | `Fixes` | back — the tagged bullets, joined by `<br>` |

**The front is his own wrong sentence and the back is the fix**, so the card asks him to
produce the correction rather than recognise it. Recognising a correction teaches nothing —
it always looks obvious once it is on the screen.

**`Original` is copied character for character**, typos included. It is the evidence;
cleaning it up would delete the very thing the row exists to record.

**Rows are a log first, cards second.** Reading a day's rows to see what was corrected is the
main use, so nothing is filtered or merged — every correction of that day gets its row.

**A day with no corrections has no rows.** Writing in Vietnamese, or writing English that was
already fine, produces no `Fixes` bullets and therefore nothing to store.

Quotes inside `Fixes` are written as `&quot;` — the field is rendered as HTML.

## 5. Publishing to Anki

sheets2anki reads the `.xlsx` itself, so the workbook is both what an agent writes and what
Anki fetches — there is no export step between them. `validate_url()` takes any https address
whose path ends in `.xlsx` or `.xlsm`, and `normalize_file_url()` rewrites a GitHub `/blob/`
address to `raw.githubusercontent.com`, which is the host that serves bytes rather than a page.

**Publishing is therefore `git push`**, and the deck URL is the address the browser shows:

```
https://github.com/<owner>/<repo>/blob/main/my-vocab-sheet.xlsx#sheet=vocab
```

**The `#sheet=` fragment names the tab** — one file, two decks, `#sheet=grammar` for the other.
Leave it off and the add-on syncs the first sheet, which is `vocab` by accident rather than by
instruction. A fragment and not a query parameter because the same URL still opens the file in
a browser.

**The repository has to be public.** `raw.githubusercontent.com` answers 404 for a private repo
unless the request carries a token, and the add-on sends none.

**A push lands within five minutes, not instantly** — the raw host replies `cache-control:
max-age=300`, so a sync straight after a push can still be served the previous bytes. It also
replies `access-control-allow-origin: *`, which is why <https://tannc28.github.io/sheets2anki/>
can read the same URL from the browser; that page runs the add-on's own code, so its column
mapping and warnings are the real ones.

Re-importing updates notes instead of duplicating them, because identity comes from `ID`.
