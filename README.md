# annotated-diff

A Claude Code skill (and a standalone script) that renders one self-contained HTML page showing
what changed and why: whole files GitHub-style (red/green lines, old and new line numbers, full
context) with review notes as cards in the right margin, next to the lines they explain.

It is meant for a human reviewing an agent's work in a browser instead of a terminal.

The page has:

- margin cards aligned with their lines, stacked so they never overlap, and linked to their lines
  on hover (click a card to jump to its line, click a line's pin to jump to its card);
- "Changes only": hides unchanged lines behind expandable "N unchanged lines" bars;
- "Notes inline": cards as rows under their lines (automatic on narrow screens);
- ↑/↓ note navigation, also with the j/k keys;
- jump links per file, and a panel for notes not tied to a line, including notes whose anchor was
  not found (marked "line not found");
- light and dark themes that follow the system setting;
- no external requests: one HTML file with inline CSS and JS.

The script is a single file, `python3` 3.9+ with the standard library only (plus `git` in git
mode).

## Install

### As a Claude Code plugin

The repository is a plugin marketplace. In a Claude Code session:

```
/plugin marketplace add thomas-lane/annotated-diff
/plugin install annotated-diff@annotated-diff
```

Or from a shell:

```bash
claude plugin marketplace add thomas-lane/annotated-diff
claude plugin install annotated-diff@annotated-diff
```

The repository is private, so the machine needs git access to it: an SSH key for GitHub, or for
HTTPS run `gh auth login` and `gh auth setup-git` first (Claude Code runs `git` without prompts).
Set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` to skip the SSH attempt.

Claude uses the skill on its own when you ask to review changes in the browser, or invoke it
directly with `/annotated-diff:annotated-diff`. The plugin sets no `version`, so an update
always fetches the latest commit: `/plugin marketplace update annotated-diff`.

### Manually, as a personal skill

```bash
git clone git@github.com:thomas-lane/annotated-diff.git
cp -R annotated-diff/plugins/annotated-diff/skills/annotated-diff ~/.claude/skills/
```

The skill is then available in every project as `/annotated-diff`.

## Usage

Claude follows `plugins/annotated-diff/skills/annotated-diff/SKILL.md`: pick the base the
reviewer last saw, write a notes file, render, fix any unmatched anchors, open the page. The
script also works on its own:

```bash
S=plugins/annotated-diff/skills/annotated-diff/scripts/annotated_diff.py

# Working tree (including untracked files) against a commit
python3 $S --repo . --base main --notes notes.json --open

# A commit range, limited to some paths
python3 $S --repo . --base v1.2 --head HEAD 'src/*.py' docs/ --exclude 'docs/generated/*' --out review.html

# A branch against where it forked from main
python3 $S --repo . --base main --head feature --merge-base --notes notes.json

# Without git: explicit before/after pairs (/dev/null for a missing side)
python3 $S --pair app.py old/app.py new/app.py --pair new.cfg /dev/null new/new.cfg --notes notes.json
```

`python3 $S --help` lists every option. Exit status: 0 on success, 1 with `--strict` when a note
could not be placed or an anchor is ambiguous, 2 on bad input.

### Notes file

```json
{
  "comments": [
    {"file": "src/app.py", "anchor": "def load(", "label": "Bug fix",
     "note": "Why this changed. `code` and **bold** are rendered."},
    {"file": "src/app.py", "anchor": "-old_call(x)", "note": "Anchored to a removed line."},
    {"file": "README.md", "note": "No anchor: a note about the whole file."}
  ],
  "general": [{"title": "Not tied to a line", "note": "..."}, "or a plain string"]
}
```

- `anchor` is a substring of exactly one added line of that file's diff; prefix `-` for a removed
  line, `+` to force an added line that itself starts with `-`. If nothing matches exactly,
  whitespace differences are ignored.
- `label` (alias: `rule`) is an optional tag shown on the card; each label gets a stable color.
- Notes that cannot be placed are printed to stderr with the reason and listed on the page as
  "line not found". Ambiguous anchors are placed on the first match and reported.
- Repeat `--notes` to merge several files.

## Demo

`examples/demo/` has a before/after pair of a small module and a notes file:

```bash
examples/demo/render.sh /tmp/demo.html --open
```

## Tests

```bash
python3 -m unittest discover -s tests
```

## Layout

```
.claude-plugin/marketplace.json            marketplace listing this repository's plugin
plugins/annotated-diff/
  .claude-plugin/plugin.json               plugin manifest
  skills/annotated-diff/SKILL.md           instructions Claude follows
  skills/annotated-diff/scripts/annotated_diff.py   the generator
examples/demo/                             before/after files, notes.json, render.sh
tests/test_annotated_diff.py               unit tests (stdlib unittest)
```
