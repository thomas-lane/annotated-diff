---
name: annotated-diff
description: Show a human reviewer what was changed and why, as a browser page of whole-file diffs (GitHub-style red/green lines) with review notes in the margin next to the changed lines. Use when the user wants to review changes, a diff, a branch or a PR in the browser instead of the terminal, asks for an annotated diff, margin comments or "explain your changes line by line", or wants a walkthrough of edits an agent made across several files.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/annotated_diff.py *)
---

# Annotated diff

`${CLAUDE_SKILL_DIR}/scripts/annotated_diff.py` renders one self-contained HTML page: each changed
file in full, GitHub-style, with your notes as cards in the right margin beside the lines they
explain. The page has a "Changes only" toggle, an inline-notes mode (automatic on narrow
screens), ↑/↓ note navigation (j/k keys), and a panel for notes that are not tied to a line. It
needs only `python3` (3.9+, standard library) and, in git mode, `git`.

## 1. Choose what to compare

The base is **the state the user last saw**, so the page shows exactly what is new to them.

- Uncommitted edits on top of the current commit: `--base HEAD` (the default), no `--head`. The
  working tree is compared, including untracked files (`--no-untracked` to leave them out).
- You committed during the session: the commit from before your first change. Run
  `git rev-parse HEAD` before editing when you expect to show a review later.
- A follow-up round after the user reviewed an earlier page: the commit (or snapshot) that page
  showed, not the original starting point.
- A branch or PR: `--base main --merge-base` (with `--head BRANCH` if it is not checked out).
- Not a git repository, or the reviewed version is not in git: copy the files before editing and
  use file mode: `--pair LABEL BEFORE AFTER` per file (`/dev/null` for an added or deleted side).

Limit the page to the files that matter with pathspecs after the options (`'src/*.py' docs/`),
and `--exclude GLOB` for generated files and lockfiles. Binary files and files over
`--max-bytes` are listed as "not shown".

## 2. Write the notes

Write a JSON file somewhere outside the repository (a scratch or temp directory):

```json
{
  "comments": [
    {"file": "src/store.py", "anchor": "def restock(", "label": "Bug fix",
     "note": "Adds the received units instead of overwriting the count; callers pass a delta."},
    {"file": "src/store.py", "anchor": "-if name in cache:", "label": "Refactor",
     "note": "The cache check moved into `lookup()`, so both callers share it."},
    {"file": "docs/setup.md", "label": "Docs", "note": "A note about the whole file."}
  ],
  "general": [
    {"title": "Not done", "note": "The migration script is a separate change."},
    "A plain string also works."
  ]
}
```

What makes a good note:

- One note per meaningful change, not per line. Group a block of related lines under one note on
  its first or most telling line. Skip trivial edits (formatting, renames the diff makes obvious).
- Say what changed **and why**: the bug, the reason, the consequence. Do not restate the diff.
- Concise: one to three sentences. Mention risks, behavior changes, follow-ups and how it was
  tested when relevant. Be honest about anything uncertain or not verified.
- `label` is a short tag shown on the card (e.g. "Bug fix", "Refactor", "Docs", "Test",
  "Question", "Behavior change"). Use a small consistent set; each label gets its own color.
  `rule` is accepted as an alias. Optional.
- Formatting: `` `code` ``, `**bold**`, and blank lines between paragraphs. Everything else is
  shown as plain text (HTML is escaped).

Anchors:

- `file` is the path as shown in the diff (repo-relative in git mode, the LABEL in file mode).
  For a renamed file, the old or new path works.
- `anchor` is a substring of exactly **one added line** in that file's diff. Copy a distinctive
  fragment of the line (the shortest unambiguous one); it must not span lines.
- Prefix with `-` to anchor to a **removed** line (`"-old_call(x)"`). If an added line itself
  starts with `-`, prefix with `+` (`"+- list item"`).
- Anchors must be on changed lines; unchanged lines cannot be anchored. A note about an
  unchanged line belongs on the nearest change or in `general`.
- Leave out `anchor` for a note about the whole file (shown under the file name).
- Notes with no `file` and things not tied to one line (design decisions, what was not changed,
  open questions) go in `general`.

Several notes files can be passed (repeat `--notes`); they are merged. Notes are numbered in
reading order.

## 3. Render, check, open

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/annotated_diff.py" --repo . --base <REF> \
  --notes /path/to/notes.json --title "What changed" --subtitle "since \`<REF>\`" \
  --out /path/to/review.html --open
```

File mode: replace `--repo/--base` with `--pair LABEL BEFORE AFTER` (repeatable).

Other options: `--head REF`, `--merge-base`, `--changes-only` (start collapsed, for large
files), `--context N`, `--strict` (exit 1 if any note could not be placed or an anchor is
ambiguous). Without `--out` the page goes to `annotated-diff.html` in the system temp directory.
Run with `--help` for everything.

Then:

1. Read stderr. Every note that could not be placed is printed with the reason (anchor not
   found, matches a removed or unchanged line, file not in the diff) and is shown on the page
   under "line not found". Ambiguous anchors are reported too. Fix the anchors and run again until
   stderr is clean (or run with `--strict` to make it fail).
2. Open the page (`--open`, or `open`/`xdg-open` the file) and give the user the path.
3. Optional: if a browser automation tool is available, screenshot the page to check it renders
   (cards beside their lines, no "line not found" entries you did not intend).

The page is static and self-contained: it makes no network requests, so it can be attached or
shared as a single file.
