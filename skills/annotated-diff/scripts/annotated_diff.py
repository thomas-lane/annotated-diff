#!/usr/bin/env python3
"""Render whole-file diffs with review notes as one self-contained HTML page.

Each file is shown GitHub-style (red/green lines, old and new line numbers, the full file as
context). Notes are anchored to changed lines and shown as cards in a right-hand margin, next to
their lines. Notes that are not tied to a line, and notes whose anchor matched nothing, are listed
in a panel at the top of the page.

Two ways to choose what to compare:

  git mode   annotated_diff.py --repo PATH --base REF [--head REF] [PATHSPEC ...]
             (no --head: the working tree, including untracked files)
  file mode  annotated_diff.py --pair LABEL BEFORE AFTER [--pair ...]
             (use /dev/null as BEFORE or AFTER for an added or deleted file)

Notes come from one or more JSON files (--notes, repeatable):

  {"comments": [{"file": "src/app.py", "anchor": "def load(", "label": "Bug fix",
                 "note": "Why this line changed."}],
   "general":  [{"title": "Not tied to a line", "note": "..."}, "or a plain string"]}

An anchor is a substring of exactly one added line in that file's diff. Prefix it with "-" to
anchor to a removed line instead, or with "+" to force an added line (e.g. "+-x" matches an
added line containing "-x"). "rule" is accepted as an alias of "label".

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import webbrowser
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROG = "annotated-diff"
DEFAULT_MAX_BYTES = 2_000_000

# ---------------------------------------------------------------------------------------------
# Data model


@dataclass
class FileDiff:
    """One file to show: its display label, both versions, and how it changed."""

    label: str
    before: str
    after: str
    status: str = "modified"  # modified | added | deleted | renamed
    old_label: Optional[str] = None  # previous path of a renamed file

    def names(self) -> Tuple[str, ...]:
        """Names a note's "file" field may use to refer to this file."""
        names = [self.label]
        if self.old_label:
            names += [self.old_label, f"{self.old_label} → {self.label}"]
        return tuple(names)


@dataclass
class Skipped:
    """A changed file that is not rendered (binary, too large, unreadable, no text change)."""

    label: str
    reason: str


@dataclass
class Note:
    text: str
    label: str = ""
    file: Optional[str] = None
    anchor: Optional[str] = None
    title: str = ""
    source: str = ""  # which notes file it came from, for messages
    order: int = 0  # input order, used to break ties
    # Filled in while matching:
    num: int = 0
    file_idx: Optional[int] = None
    row: Optional[int] = None
    problem: str = ""  # why an anchored note could not be placed
    warning: str = ""  # placed, but something looks off (e.g. ambiguous anchor)


Row = Tuple[str, Optional[int], Optional[int], str]  # (kind, old line no, new line no, text)


class UsageError(Exception):
    """Bad input; reported without a traceback and exit status 2."""


# ---------------------------------------------------------------------------------------------
# Diffing and anchor matching


def diff_rows(before: str, after: str) -> List[Row]:
    """Whole-file diff rows: every line of both versions, in order.

    kind is "ctx" (unchanged), "del" (only in before) or "add" (only in after). Within a changed
    block, removed lines come before added lines, as in a unified diff.
    """
    a, b = before.splitlines(), after.splitlines()
    rows: List[Row] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(("ctx", i1 + k + 1, j1 + k + 1, b[j1 + k]))
        else:
            for k in range(i1, i2):
                rows.append(("del", k + 1, None, a[k]))
            for k in range(j1, j2):
                rows.append(("add", None, k + 1, b[k]))
    return rows


def _squash(s: str) -> str:
    return " ".join(s.split())


def parse_anchor(anchor: str) -> Tuple[str, str]:
    """Split an anchor into (row kind, needle): "-x" -> removed line, "+x" or "x" -> added line."""
    if anchor.startswith("-"):
        return "del", anchor[1:]
    if anchor.startswith("+"):
        return "add", anchor[1:]
    return "add", anchor


def match_anchor(rows: Sequence[Row], anchor: str) -> Tuple[Optional[int], int, str]:
    """Find the row an anchor points at.

    Returns (row index or None, number of matching rows, problem). An exact substring match is
    tried first; if none, a match that ignores differences in whitespace.
    """
    kind, needle = parse_anchor(anchor)
    side = "removed" if kind == "del" else "added"
    if not needle.strip():
        return None, 0, "empty anchor"
    if "\n" in needle:
        return None, 0, "anchor spans several lines; use a substring of one line"
    hits = [i for i, r in enumerate(rows) if r[0] == kind and needle in r[3]]
    if not hits:
        squashed = _squash(needle)
        hits = [i for i, r in enumerate(rows) if r[0] == kind and squashed in _squash(r[3])]
    if not hits:
        other = "del" if kind == "add" else "add"
        hint = ""
        if any(r[0] == other and needle in r[3] for r in rows):
            hint = (" (it matches a removed line: prefix the anchor with \"-\")" if kind == "add"
                    else " (it matches an added line: drop the leading \"-\")")
        elif any(r[0] == "ctx" and needle in r[3] for r in rows):
            hint = " (it matches an unchanged line; anchors must be on changed lines)"
        return None, 0, f"no {side} line contains it{hint}"
    return hits[0], len(hits), ""


# ---------------------------------------------------------------------------------------------
# Notes


def _as_text(value, where: str) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    raise UsageError(f"{where}: expected a string, got {type(value).__name__}")


def load_notes(paths: Sequence[str]) -> Tuple[List[Note], List[Note]]:
    """Read and merge notes files. Returns (line/file notes, general notes)."""
    comments: List[Note] = []
    general: List[Note] = []
    order = 0
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UsageError(f"notes file not found: {path}")
        except json.JSONDecodeError as e:
            raise UsageError(f"{path}: invalid JSON: {e}")
        if isinstance(data, list):  # a bare list is a list of comments
            data = {"comments": data}
        if not isinstance(data, dict):
            raise UsageError(f"{path}: expected an object with \"comments\" and/or \"general\"")
        unknown = set(data) - {"comments", "general", "notes"}
        if unknown:
            warn(f"{path}: ignoring unknown top-level keys: {', '.join(sorted(unknown))}")
        for i, c in enumerate(list(data.get("comments") or []) + list(data.get("notes") or [])):
            where = f"{path}: comments[{i}]"
            if isinstance(c, str):
                general.append(Note(text=c, source=path, order=order))
                order += 1
                continue
            if not isinstance(c, dict):
                raise UsageError(f"{where}: expected an object")
            text = _as_text(c.get("note", c.get("text")), where)
            if not text:
                raise UsageError(f"{where}: missing \"note\"")
            anchor = c.get("anchor")
            comments.append(Note(
                text=text,
                label=_as_text(c.get("label", c.get("rule")), where),
                file=_as_text(c.get("file"), where) or None,
                anchor=_as_text(anchor, where) if anchor not in (None, "") else None,
                title=_as_text(c.get("title"), where),
                source=path,
                order=order,
            ))
            order += 1
        for i, g in enumerate(data.get("general") or []):
            where = f"{path}: general[{i}]"
            if isinstance(g, dict):
                text = _as_text(g.get("note", g.get("text")), where)
                general.append(Note(text=text, title=_as_text(g.get("title"), where),
                                    label=_as_text(g.get("label", g.get("rule")), where),
                                    source=path, order=order))
            else:
                general.append(Note(text=_as_text(g, where), source=path, order=order))
            order += 1
    return comments, general


def place_notes(files: Sequence[FileDiff], rows_by_file: Sequence[List[Row]], notes: List[Note]):
    """Resolve each note's file and anchor, then number all notes in reading order.

    Returns (placed notes, unplaced notes). Placed notes have file_idx set, and row set unless
    they are file-level notes (a file but no anchor). Notes with neither are general notes.
    """
    by_name: Dict[str, int] = {}
    for idx, f in enumerate(files):
        for name in f.names():
            by_name.setdefault(name, idx)
    placed, unplaced = [], []
    for n in notes:
        if n.file is None:
            if n.anchor:
                n.problem = "anchor given without a \"file\""
                unplaced.append(n)
            else:
                unplaced.append(n)  # no file, no anchor: a general note
            continue
        key = n.file[2:] if n.file.startswith("./") else n.file
        idx = by_name.get(n.file, by_name.get(key))
        if idx is None:
            n.problem = "file is not in the diff"
            unplaced.append(n)
            continue
        n.file_idx = idx
        if n.anchor is None:
            placed.append(n)  # file-level note
            continue
        row, count, problem = match_anchor(rows_by_file[idx], n.anchor)
        if row is None:
            n.problem = problem
            unplaced.append(n)
            continue
        n.row = row
        if count > 1:
            r = rows_by_file[idx][row]
            line = r[2] if r[2] is not None else r[1]
            n.warning = f"matches {count} lines; placed on the first (line {line}). Make the anchor longer."
        placed.append(n)
    placed.sort(key=lambda n: (n.file_idx, -1 if n.row is None else n.row, n.order))
    return placed, unplaced


# ---------------------------------------------------------------------------------------------
# Inputs: git and explicit pairs


def run_git(repo: str, *args: str) -> bytes:
    try:
        p = subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise UsageError("git is not installed or not on PATH")
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip()
        raise UsageError(f"git {' '.join(args)} failed: {msg}")
    return p.stdout


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def collect_git(repo: str, base: str, head: Optional[str], pathspecs: Sequence[str],
                excludes: Sequence[str], untracked: bool, merge_base: bool, max_bytes: int):
    """Changed files between base and head (or the working tree). Returns (files, skipped, desc)."""
    top = _decode(run_git(repo, "rev-parse", "--show-toplevel")).strip()
    for ref in [base] + ([head] if head else []):
        try:
            run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except UsageError:
            raise UsageError(f"not a commit in {top}: {ref}")
    if merge_base:
        base = _decode(run_git(repo, "merge-base", base, head or "HEAD")).strip()
    specs = list(pathspecs) + [f":(exclude){x}" for x in excludes]
    # Run from the top level so pathspecs and output paths are both relative to the repo root.
    args = ["diff", "--no-color", "--no-ext-diff", "-M", "--name-status", "-z", base]
    if head:
        args.append(head)
    out = run_git(top, *args, "--", *specs).split(b"\0")
    entries: List[Tuple[str, Optional[str], str]] = []  # (status letter, old path, new path)
    i = 0
    while i < len(out) and out[i]:
        status = _decode(out[i])
        if status[0] in "RC":
            entries.append((status[0], _decode(out[i + 1]), _decode(out[i + 2])))
            i += 3
        else:
            entries.append((status[0], None, _decode(out[i + 1])))
            i += 2
    if not head and untracked:
        extra = run_git(top, "ls-files", "-z", "--others", "--exclude-standard", "--", *specs)
        entries += [("A", None, _decode(p)) for p in extra.split(b"\0") if p]

    def blob(ref: str, path: str) -> bytes:
        return run_git(top, "cat-file", "blob", f"{ref}:{path}")

    def worktree(path: str) -> bytes:
        p = Path(top, path)
        if p.is_symlink():
            return os.readlink(p).encode()
        return p.read_bytes()

    files: List[FileDiff] = []
    skipped: List[Skipped] = []
    for status, old, new in entries:
        label = new
        try:
            if status == "A":
                before_b = b""
                after_b = blob(head, new) if head else worktree(new)
            elif status == "D":
                before_b, after_b = blob(base, new), b""
            elif status in "RC":
                before_b = blob(base, old)
                after_b = blob(head, new) if head else worktree(new)
            else:  # M, T (type change), U (unmerged)
                before_b = blob(base, new)
                after_b = blob(head, new) if head else worktree(new)
        except (UsageError, OSError) as e:
            skipped.append(Skipped(label, f"could not read it ({str(e).splitlines()[0][:160]})"))
            continue
        if is_binary(before_b) or is_binary(after_b):
            skipped.append(Skipped(label, "binary file"))
            continue
        if max(len(before_b), len(after_b)) > max_bytes:
            size = max(len(before_b), len(after_b)) // 1024
            skipped.append(Skipped(label, f"too large ({size} KB; raise --max-bytes to include it)"))
            continue
        before, after = _decode(before_b), _decode(after_b)
        kind = {"A": "added", "D": "deleted", "R": "renamed", "C": "copied"}.get(status, "modified")
        if before == after and kind not in ("renamed", "copied"):
            skipped.append(Skipped(label, "no text change (mode or line-ending change only)"))
            continue
        files.append(FileDiff(label, before, after, kind, old if status in "RC" else None))
    short = _decode(run_git(top, "rev-parse", "--short", base)).strip()
    desc = f"{Path(top).name}: {base if base != short and len(base) < 40 else short}"
    desc += f" → {head}" if head else " → working tree"
    return files, skipped, desc


def collect_pairs(pairs: Sequence[Sequence[str]], max_bytes: int):
    files: List[FileDiff] = []
    skipped: List[Skipped] = []
    for label, before_path, after_path in pairs:
        data = []
        for p in (before_path, after_path):
            if p == os.devnull:
                data.append(None)
                continue
            try:
                data.append(Path(p).read_bytes())
            except OSError as e:
                raise UsageError(f"--pair {label}: cannot read {p}: {e.strerror}")
        before_b, after_b = (d or b"" for d in data)
        if is_binary(before_b) or is_binary(after_b):
            skipped.append(Skipped(label, "binary file"))
            continue
        if max(len(before_b), len(after_b)) > max_bytes:
            skipped.append(Skipped(label, "too large (raise --max-bytes to include it)"))
            continue
        status = "added" if data[0] is None else "deleted" if data[1] is None else "modified"
        files.append(FileDiff(label, _decode(before_b), _decode(after_b), status))
    return files, skipped


# ---------------------------------------------------------------------------------------------
# HTML rendering

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def fmt(text: str) -> str:
    """Escape text, then render `code`, **bold**, line breaks and blank-line paragraphs."""

    def inline(s: str) -> str:
        parts = _CODE_SPAN.split(s)
        out = []
        for k, part in enumerate(parts):
            if k % 2:
                out.append(f"<code>{html.escape(part)}</code>")
            else:
                out.append(_BOLD.sub(r"<b>\1</b>", html.escape(part)))
        return "".join(out).replace("\n", "<br>")

    paras = [p.strip("\n") for p in re.split(r"\n\s*\n", text.strip())]
    return "".join(f"<p>{inline(p)}</p>" for p in paras if p)


def tag_html(label: str) -> str:
    if not label:
        return ""
    hue = zlib.crc32(label.encode("utf-8")) % 360
    return f'<span class="tag" style="--h:{hue}">{html.escape(label)}</span>'


def card_html(n: Note, *, card_id: str, row_id: str = "",
              where: str = "") -> str:
    attrs = f' id="{card_id}" data-c="{n.num}"'
    if row_id:
        attrs += f' data-row="{row_id}"'
    head = f'<span class="nid">#{n.num}</span>{tag_html(n.label)}'
    if n.title:
        head += f'<span class="ntitle">{fmt_inline(n.title)}</span>'
    if where:
        head += where
    return f'<div class="note"{attrs}><div class="nhead">{head}</div><div class="nbody">{fmt(n.text)}</div></div>'


def fmt_inline(text: str) -> str:
    html_ = fmt(text)
    return html_[3:-4] if html_.startswith("<p>") and html_.count("<p>") == 1 else html_


def render_file(idx: int, f: FileDiff, rows: List[Row], notes: List[Note]) -> str:
    line_notes: Dict[int, List[Note]] = {}
    file_notes = []
    for n in notes:
        if n.row is None:
            file_notes.append(n)
        else:
            line_notes.setdefault(n.row, []).append(n)
    width = max(2, len(str(max(len(f.before.splitlines()), len(f.after.splitlines()), 1))))
    body = []
    for i, (kind, old, new, text) in enumerate(rows):
        sign = {"ctx": " ", "add": "+", "del": "−"}[kind]
        ns = line_notes.get(i, [])
        rid = f"f{idx}-r{i}"
        pins = "".join(f'<a class="pin" href="#c-{n.num}" data-c="{n.num}">{n.num}</a>' for n in ns)
        attr = f' data-anchor="{" ".join(str(n.num) for n in ns)}"' if ns else ""
        code = html.escape(text) or "&#8203;"
        body.append(f'<tr class="ln {kind}" id="{rid}"{attr}><td class="num">{old or ""}</td>'
                    f'<td class="num">{new or ""}</td><td class="sign">{sign}</td>'
                    f'<td class="code">{code}{pins}</td></tr>')
        for n in ns:
            body.append(f'<tr class="inline-note"><td colspan="4">'
                        f'{card_html(n, card_id=f"ci-{n.num}", row_id=rid)}</td></tr>')
    if not rows:
        body.append('<tr class="empty"><td colspan="4">(empty file)</td></tr>')
    margin = "".join(card_html(n, card_id=f"c-{n.num}", row_id=f"f{idx}-r{n.row}")
                     for n in notes if n.row is not None)
    n_add = sum(r[0] == "add" for r in rows)
    n_del = sum(r[0] == "del" for r in rows)
    name = html.escape(f.label)
    if f.status in ("renamed", "copied") and f.old_label:
        name = f'{html.escape(f.old_label)} → {name}'
    badge = f'<span class="badge {f.status}">{f.status}</span>' if f.status != "modified" else ""
    fnotes = ""
    if file_notes:
        fnotes = '<div class="file-notes">' + "".join(
            card_html(n, card_id=f"c-{n.num}") for n in file_notes) + "</div>"
    return f"""
<section class="file" id="file-{idx}">
  <header class="file-head"><span class="fname">{name}</span>{badge}
    <span class="stat"><span class="plus">+{n_add}</span> <span class="minus">−{n_del}</span></span></header>
  {fnotes}<div class="file-body">
    <table class="diff" style="--numw:{width}"><colgroup><col class="cnum"><col class="cnum"><col class="csign"><col></colgroup><tbody>{''.join(body)}</tbody></table>
    <aside class="notes">{margin}</aside>
  </div>
</section>"""


def render_page(files: Sequence[FileDiff], skipped: Sequence[Skipped], comments: List[Note],
                general: List[Note], *, title: str, subtitle: str = "", context: int = 3,
                changes_only: bool = False):
    """Build the page. Returns (html, placed notes, unplaced notes). Numbers all notes."""
    rows_by_file = [diff_rows(f.before, f.after) for f in files]
    placed, unplaced = place_notes(files, rows_by_file, comments)
    num = 0
    for n in placed:
        num += 1
        n.num = num
    plain_general = [n for n in unplaced if not n.problem] + list(general)
    plain_general.sort(key=lambda n: n.order)
    problems = [n for n in unplaced if n.problem]
    for n in plain_general + problems:
        num += 1
        n.num = num

    sections = "".join(
        render_file(i, f, rows_by_file[i], [n for n in placed if n.file_idx == i])
        for i, f in enumerate(files))

    panel = ""
    if plain_general or problems:
        items = []
        for n in plain_general:
            where = f'<span class="where">{html.escape(n.file)}</span>' if n.file else ""
            items.append(card_html(n, card_id=f"c-{n.num}", where=where))
        for n in problems:
            where = (f'<span class="where">{html.escape(n.file or "(no file)")}</span>'
                     f'<span class="lost" title="{html.escape(n.problem)}">line not found</span>'
                     f'<code class="anchor">{html.escape(n.anchor or "")}</code>')
            items.append(card_html(n, card_id=f"c-{n.num}", where=where))
        count = len(plain_general) + len(problems)
        panel = (f'<details class="general" open><summary>Notes not tied to a line ({count})</summary>'
                 f'{"".join(items)}</details>')
    skipped_html = ""
    if skipped:
        skipped_html = ('<details class="skipped"><summary>Changed files not shown '
                        f'({len(skipped)})</summary><ul>' + "".join(
                            f'<li><code>{html.escape(s.label)}</code> {html.escape(s.reason)}</li>'
                            for s in skipped) + "</ul></details>")
    nav = ""
    if len(files) > 1:
        links = []
        for i, f in enumerate(files):
            rows = rows_by_file[i]
            k = sum(1 for n in placed if n.file_idx == i)
            links.append(f'<a href="#file-{i}">{html.escape(f.label)}'
                         f'<span class="plus">+{sum(r[0] == "add" for r in rows)}</span>'
                         f'<span class="minus">−{sum(r[0] == "del" for r in rows)}</span>'
                         + (f'<span class="ncount">{k} note{"s" if k != 1 else ""}</span>' if k else "")
                         + "</a>")
        nav = f'<nav class="filenav">{"".join(links)}</nav>'
    empty = '<p class="nothing">No changed text files.</p>' if not files else ""
    stats = f"{len(files)} file{'s' if len(files) != 1 else ''} · {num} note{'s' if num != 1 else ''}"
    body_cls = ' class="changes-only"' if changes_only else ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="{PROG}">
<title>{html.escape(title)}</title>
<style>{CSS}</style></head>
<body{body_cls} data-context="{int(context)}">
<div class="top" id="top">
  <h1>{html.escape(title)}</h1>
  {f'<span class="sub">{fmt_inline(subtitle)}</span>' if subtitle else ''}
  <span class="sub stats">{stats}</span>
  <span class="spacer"></span>
  <span class="controls">
    <label><input type="checkbox" id="only"{' checked' if changes_only else ''}> Changes only</label>
    <label><input type="checkbox" id="inl"> Notes inline</label>
    <span class="navbtns"><button id="prev" type="button" title="Previous note (k)">↑ Note</button>
    <button id="next" type="button" title="Next note (j)">↓ Note</button>
    <span class="pos" id="pos"></span></span>
  </span>
</div>
<main>{nav}{panel}{skipped_html}{empty}{sections}</main>
<script>{JS}</script>
</body></html>
"""
    return page, placed, problems


CSS = r"""
:root {
  --bg:#ffffff; --fg:#1f2328; --muted:#656d76; --border:#d0d7de; --head:#f6f8fa;
  --add-bg:#e6ffec; --add-num:#ccffd8; --add-fg:#1a7f37; --del-bg:#ffebe9; --del-num:#ffd7d5; --del-fg:#cf222e;
  --note-bg:#fffbe6; --note-border:#e3c766; --pin:#e3c766; --accent:#0969da; --hl:#fff8c5; --warn:#bc4c00;
  --tag-l:92%; --tag-fg-l:28%; --tag-b-l:75%;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0d1117; --fg:#e6edf3; --muted:#8d96a0; --border:#30363d; --head:#161b22;
  --add-bg:#12261e; --add-num:#1b4721; --add-fg:#3fb950; --del-bg:#25171c; --del-num:#542426; --del-fg:#f85149;
  --note-bg:#1f1d12; --note-border:#6e5d1e; --pin:#d4a72c; --accent:#4493f8; --hl:#3a3212; --warn:#f0883e;
  --tag-l:20%; --tag-fg-l:80%; --tag-b-l:35%; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
code { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; background:rgba(127,127,127,.15); padding:0 3px; border-radius:3px; }
.top { position:sticky; top:0; z-index:5; background:var(--head); border-bottom:1px solid var(--border); padding:10px 16px; display:flex; gap:8px 16px; align-items:center; flex-wrap:wrap; }
.top h1 { font-size:15px; margin:0; font-weight:600; }
.top .sub { color:var(--muted); font-size:13px; }
.top .spacer { flex:1; }
.top .controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; }
.top .navbtns { display:flex; align-items:center; gap:8px; white-space:nowrap; }
.top label, .top button { font-size:13px; color:var(--fg); white-space:nowrap; }
.top label:has(input:disabled) { color:var(--muted); }
.top button { background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:3px 10px; cursor:pointer; }
.top button:hover { border-color:var(--accent); }
.top .pos { color:var(--muted); font-size:12px; min-width:3em; font-variant-numeric:tabular-nums; }
main { padding:16px; max-width:1500px; margin:0 auto; }
.file { border:1px solid var(--border); border-radius:6px; margin-bottom:24px; scroll-margin-top:calc(var(--top-h, 49px) + 8px); }
/* Sticky file-name bar: sits directly under the sticky top bar. --top-h is the top bar's measured
   height, kept current by the script (it changes when the bar wraps). */
.file-head { background:var(--head); border-bottom:1px solid var(--border); padding:8px 12px; display:flex; gap:10px; align-items:center; position:sticky; top:var(--top-h, 49px); z-index:3; border-radius:6px 6px 0 0; }
.fname { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-weight:600; font-size:13px; overflow-wrap:anywhere; }
.file-head .stat { margin-left:auto; white-space:nowrap; }
.badge { font-size:11px; border:1px solid var(--border); border-radius:10px; padding:0 7px; color:var(--muted); }
.badge.added { color:var(--add-fg); border-color:var(--add-fg); } .badge.deleted { color:var(--del-fg); border-color:var(--del-fg); }
.plus { color:var(--add-fg); font-weight:600; } .minus { color:var(--del-fg); font-weight:600; }
.file-notes { padding:10px; border-bottom:1px solid var(--border); display:grid; gap:8px; }
.file-notes .note { position:static; }
.file-body { display:grid; grid-template-columns:minmax(0,1fr) 360px; }
/* align-self:start: when the stacked notes are taller than the diff, the table must not stretch
   (a stretched table spreads the extra height over its rows, which moves the cards again). */
table.diff { align-self:start; border-collapse:collapse; width:100%; font:12.5px/20px ui-monospace,SFMono-Regular,Menlo,monospace; table-layout:fixed; tab-size:4; }
/* Column widths come from the colgroup: with table-layout:fixed the first row would otherwise decide
   them, and in "Changes only" view that can be a full-width fold bar. */
.diff col.cnum { width:calc(var(--numw) * 1ch + 16px); } .diff col.csign { width:18px; }
.diff td.num { text-align:right; padding:0 8px; color:var(--muted); user-select:none; vertical-align:top; }
.diff td.sign { text-align:center; user-select:none; vertical-align:top; color:var(--muted); }
.diff td.code { white-space:pre-wrap; word-break:break-word; padding-right:12px; vertical-align:top; }
tr.add { background:var(--add-bg); } tr.add td.num { background:var(--add-num); } tr.add td.sign { color:var(--add-fg); }
tr.del { background:var(--del-bg); } tr.del td.num { background:var(--del-num); } tr.del td.sign { color:var(--del-fg); }
tr.hl td { box-shadow:inset 0 0 0 9999px var(--hl); }
tr.empty td { color:var(--muted); padding:8px 12px; font-style:italic; }
tr.fold td { background:var(--head); color:var(--muted); text-align:center; font:12px/22px -apple-system,BlinkMacSystemFont,sans-serif; cursor:pointer; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }
tr.fold:hover td { color:var(--accent); }
body.changes-only tr.ln.far { display:none; }
body:not(.changes-only) tr.fold { display:none; }
.pin { display:inline-block; margin-left:8px; min-width:20px; padding:0 6px; border-radius:10px; background:var(--pin); color:#000; font:600 11px/18px -apple-system,BlinkMacSystemFont,sans-serif; text-align:center; text-decoration:none; vertical-align:1px; }
.pin.active { box-shadow:0 0 0 2px var(--accent); }
.notes { position:relative; border-left:1px solid var(--border); }
.notes .note { position:absolute; left:10px; right:10px; }
.note { background:var(--note-bg); border:1px solid var(--note-border); border-radius:8px; padding:8px 12px; font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; transition:box-shadow .15s; overflow-wrap:anywhere; }
.notes .note { cursor:pointer; }
.note.active { box-shadow:0 0 0 2px var(--accent); }
.nhead { display:flex; flex-wrap:wrap; gap:4px 8px; align-items:center; margin-bottom:4px; }
.nid { font-weight:700; }
.ntitle { font-weight:600; }
.tag { font-size:11.5px; line-height:18px; padding:0 7px; border-radius:9px; background:hsl(var(--h),60%,var(--tag-l)); color:hsl(var(--h),55%,var(--tag-fg-l)); border:1px solid hsl(var(--h),45%,var(--tag-b-l)); }
.nbody p { margin:0 0 6px; } .nbody p:last-child { margin-bottom:0; }
.where { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); }
.lost { font-size:11.5px; font-weight:600; color:var(--warn); border:1px solid var(--warn); border-radius:9px; padding:0 7px; cursor:help; }
code.anchor { color:var(--muted); }
tr.inline-note { display:none; }
tr.inline-note td { padding:6px 12px; }
body.inline-notes .notes { display:none; }
body.inline-notes tr.inline-note { display:table-row; }
body.inline-notes .file-body { grid-template-columns:minmax(0,1fr); }
.filenav { display:flex; flex-wrap:wrap; gap:6px 16px; margin:0 0 16px; font-size:13px; }
.filenav a { color:var(--accent); text-decoration:none; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.filenav a:hover { text-decoration:underline; }
.filenav .plus, .filenav .minus, .filenav .ncount { margin-left:6px; font-size:12px; }
.filenav .ncount { color:var(--muted); font-family:-apple-system,BlinkMacSystemFont,sans-serif; }
details.general, details.skipped { display:block; border-radius:8px; padding:10px 16px; margin-bottom:24px; }
details.general { border:1px solid var(--note-border); background:var(--note-bg); }
details.skipped { border:1px solid var(--border); background:var(--head); font-size:13px; }
details summary { cursor:pointer; font-weight:600; }
details.general .note { border:0; border-top:1px solid var(--note-border); border-radius:0; padding:8px 0; background:none; }
details.general .note:first-of-type { margin-top:8px; }
details.skipped ul { margin:8px 0 0; padding-left:20px; }
.nothing { color:var(--muted); }
@media (max-width:1100px) {
  .file-body { grid-template-columns:minmax(0,1fr); }
  .notes { display:none; }
  tr.inline-note { display:table-row; }
  .diff col.cnum { width:calc(var(--numw) * 1ch + 8px); } .diff td.num { padding:0 4px; }
}
"""

JS = r"""
(function () {
'use strict';
var body = document.body, root = document.documentElement;
var topBar = document.getElementById('top');
var only = document.getElementById('only'), inl = document.getElementById('inl');
var pos = document.getElementById('pos');
var narrow = window.matchMedia('(max-width: 1100px)');

// Sticky headers: each file-name bar sticks directly under the sticky top bar. The top bar's height
// depends on the viewport (its contents wrap), so measure it and publish it as --top-h. Rounded
// down so the file bar tucks under the top bar by a fraction of a pixel rather than leaving a gap.
function syncTop() {
  root.style.setProperty('--top-h', Math.floor(topBar.getBoundingClientRect().height) + 'px');
}
syncTop();
if (window.ResizeObserver) new ResizeObserver(syncTop).observe(topBar);
window.addEventListener('resize', syncTop);
window.addEventListener('load', syncTop);

function marginMode() { return !body.classList.contains('inline-notes') && !narrow.matches; }

// Margin cards: place each next to its line, pushing later cards down so they never overlap.
function layout() {
  if (!marginMode()) return;
  document.querySelectorAll('.file').forEach(function (sec) {
    var aside = sec.querySelector('.notes');
    if (!aside || !aside.children.length) return;
    var base = aside.getBoundingClientRect().top;
    var cards = Array.prototype.map.call(aside.children, function (c) {
      var row = document.getElementById(c.dataset.row);
      return { c: c, top: row ? row.getBoundingClientRect().top - base : 0 };
    }).sort(function (x, y) { return x.top - y.top; });
    var bottom = -Infinity;
    cards.forEach(function (o) {
      var t = Math.max(o.top, bottom + 8);
      o.c.style.top = t + 'px';
      bottom = t + o.c.offsetHeight;
    });
    aside.style.minHeight = bottom + 'px';
  });
}
function relayout() { requestAnimationFrame(layout); }

// "Changes only": hide unchanged lines more than N lines from a change, behind expandable bars.
function collapse() {
  var ctx = parseInt(body.dataset.context, 10) || 0;
  document.querySelectorAll('table.diff tbody').forEach(function (tb) {
    tb.querySelectorAll('tr.fold').forEach(function (f) { f.remove(); });
    var rows = Array.prototype.slice.call(tb.querySelectorAll('tr.ln'));
    var n = rows.length, near = new Array(n);
    rows.forEach(function (r, i) {
      if (r.classList.contains('ctx')) return;
      for (var k = Math.max(0, i - ctx); k <= Math.min(n - 1, i + ctx); k++) near[k] = true;
    });
    rows.forEach(function (r, i) { r.classList.toggle('far', r.classList.contains('ctx') && !near[i]); });
    for (var i = 0; i < n;) {
      if (!rows[i].classList.contains('far')) { i++; continue; }
      var j = i;
      while (j < n && rows[j].classList.contains('far')) j++;
      var block = rows.slice(i, j);
      if (block.length < 2) { block[0].classList.remove('far'); i = j; continue; }
      var f = document.createElement('tr'), td = document.createElement('td');
      f.className = 'fold'; td.colSpan = 4;
      td.textContent = '⋯ ' + block.length + ' unchanged lines';
      td.title = 'Show these lines';
      f.appendChild(td);
      f.addEventListener('click', (function (block, f) {
        return function () { block.forEach(function (r) { r.classList.remove('far'); }); f.remove(); relayout(); };
      })(block, f));
      rows[i].parentNode.insertBefore(f, rows[i]);
      i = j;
    }
  });
}
function setOnly(on) { body.classList.toggle('changes-only', on); if (on) collapse(); relayout(); }
only.addEventListener('change', function () { setOnly(only.checked); });
inl.addEventListener('change', function () { body.classList.toggle('inline-notes', inl.checked); relayout(); });
function syncNarrow() { inl.disabled = narrow.matches; inl.checked = narrow.matches || body.classList.contains('inline-notes'); relayout(); }
if (narrow.addEventListener) narrow.addEventListener('change', syncNarrow); else narrow.addListener(syncNarrow);

// Hover linking between a card, its pin and its line.
function idsOf(el) { return (el.dataset.c || el.dataset.anchor || '').split(' ').filter(Boolean); }
function activate(ids, on) {
  ids.forEach(function (id) {
    document.querySelectorAll('tr[data-anchor~="' + id + '"]').forEach(function (r) { r.classList.toggle('hl', on); });
    document.querySelectorAll('.note[data-c="' + id + '"], .pin[data-c="' + id + '"]').forEach(function (c) { c.classList.toggle('active', on); });
  });
}
var HOVER = '.note[data-c], .pin, tr[data-anchor]';
document.addEventListener('mouseover', function (e) {
  var el = e.target.closest(HOVER);
  if (el && !(e.relatedTarget && el.contains(e.relatedTarget))) activate(idsOf(el), true);
});
document.addEventListener('mouseout', function (e) {
  var el = e.target.closest(HOVER);
  if (el && !(e.relatedTarget && el.contains(e.relatedTarget))) activate(idsOf(el), false);
});
function flash(ids) { activate(ids, true); setTimeout(function () { activate(ids, false); }, 1400); }
document.addEventListener('click', function (e) {
  var pin = e.target.closest('.pin');
  if (pin) {
    e.preventDefault();
    var card = document.getElementById((marginMode() ? 'c-' : 'ci-') + pin.dataset.c);
    if (card) { card.scrollIntoView({ block: 'center', behavior: 'smooth' }); flash([pin.dataset.c]); }
    return;
  }
  var c = e.target.closest('.notes .note');
  if (!c || e.target.closest('a') || String(window.getSelection())) return;
  var row = document.getElementById(c.dataset.row);
  if (row) {
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    var k = order.indexOf(row);
    if (k >= 0) setCur(k);
    flash([c.dataset.c]);
  }
});

// Note navigation in document order: buttons and j / k.
var order = Array.prototype.slice.call(document.querySelectorAll('tr[data-anchor]'));
var cur = -1;
function setCur(k) {
  if (cur >= 0) activate(idsOf(order[cur]), false);
  cur = k;
  activate(idsOf(order[cur]), true);
  pos.textContent = (cur + 1) + ' / ' + order.length;
}
function go(d) {
  if (!order.length) return;
  setCur(cur < 0 ? (d > 0 ? 0 : order.length - 1) : (cur + d + order.length) % order.length);
  var row = order[cur];
  if (row.classList.contains('far')) { only.checked = false; setOnly(false); }
  row.scrollIntoView({ block: 'center', behavior: 'smooth' });
}
document.getElementById('next').addEventListener('click', function () { go(1); });
document.getElementById('prev').addEventListener('click', function () { go(-1); });
document.addEventListener('keydown', function (e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target;
  if (t && (t.isContentEditable || /^(TEXTAREA|SELECT)$/.test(t.tagName) || (t.tagName === 'INPUT' && t.type !== 'checkbox'))) return;
  if (e.key === 'j') go(1); else if (e.key === 'k') go(-1);
});
pos.textContent = order.length ? '0 / ' + order.length : '';

if (body.classList.contains('changes-only')) collapse();
syncNarrow();
layout();
window.addEventListener('load', layout);
window.addEventListener('resize', relayout);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
})();
"""

# ---------------------------------------------------------------------------------------------
# CLI


def warn(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="annotated_diff.py",
        description="Render whole-file diffs with review notes in the margin as one self-contained HTML page.",
        epilog=(
            "examples:\n"
            "  annotated_diff.py --repo . --base main --notes notes.json --open\n"
            "  annotated_diff.py --repo . --base HEAD~3 --head HEAD 'src/*.py' --out review.html\n"
            "  annotated_diff.py --pair app.py old/app.py new/app.py --notes notes.json\n\n"
            "notes JSON: {\"comments\": [{\"file\", \"anchor\", \"label\", \"note\"}],\n"
            "             \"general\": [{\"title\", \"note\"} or \"text\"]}\n"
            "anchor: substring of one added line; \"-text\" for a removed line; \"+text\" forces an added line.\n"
            "exit status: 0 ok; 1 with --strict when a note could not be placed or an anchor is ambiguous;\n"
            "2 bad input."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("git mode (default)")
    g.add_argument("--repo", default=".", help="repository to diff (default: current directory)")
    g.add_argument("--base", help="the version the reviewer last saw: a commit, branch or tag (default: HEAD)")
    g.add_argument("--head", help="the new version (default: the working tree, untracked files included)")
    g.add_argument("--merge-base", action="store_true",
                   help="diff from the merge base of --base and --head (like base...head)")
    g.add_argument("--no-untracked", action="store_true",
                   help="working-tree mode: leave out untracked files")
    g.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="leave out paths matching GLOB (repeatable)")
    g.add_argument("paths", nargs="*", metavar="PATHSPEC",
                   help="limit to these paths or globs, relative to the repo root (default: all changed files)")
    f = p.add_argument_group("file mode")
    f.add_argument("--pair", nargs=3, action="append", default=[], metavar=("LABEL", "BEFORE", "AFTER"),
                   help="compare two files, shown under LABEL (repeatable); /dev/null for a missing side")
    o = p.add_argument_group("notes and output")
    o.add_argument("--notes", action="append", default=[], metavar="JSON",
                   help="notes file (repeatable; merged in order)")
    o.add_argument("--title", help="page title (default: derived from the inputs)")
    o.add_argument("--subtitle", help="line shown after the title; `code` and **bold** are rendered")
    o.add_argument("--out", help="output HTML path (default: annotated-diff.html in the system temp dir)")
    o.add_argument("--open", action="store_true", help="open the page in the default browser")
    o.add_argument("--changes-only", action="store_true", help="start with unchanged lines collapsed")
    o.add_argument("--context", type=int, default=3, metavar="N",
                   help="unchanged lines kept around each change in 'Changes only' view (default: 3)")
    o.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, metavar="N",
                   help=f"skip files larger than N bytes (default: {DEFAULT_MAX_BYTES})")
    o.add_argument("--strict", action="store_true",
                   help="exit 1 if any note could not be placed or any anchor is ambiguous")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.pair:
            if args.base or args.head or args.paths or args.merge_base:
                raise UsageError("--pair cannot be combined with git options (--base, --head, paths)")
            files, skipped = collect_pairs(args.pair, args.max_bytes)
            desc = ""
            default_title = "Annotated diff"
        else:
            files, skipped, desc = collect_git(args.repo, args.base or "HEAD", args.head, args.paths,
                                               args.exclude, not args.no_untracked, args.merge_base,
                                               args.max_bytes)
            default_title = f"Changes in {desc.split(':')[0]}"
        comments, general = load_notes(args.notes)
    except UsageError as e:
        warn(str(e))
        return 2
    title = args.title or default_title
    subtitle = args.subtitle if args.subtitle is not None else (f"`{desc.split(': ', 1)[1]}`" if desc else "")
    page, placed, problems = render_page(files, skipped, comments, general, title=title,
                                         subtitle=subtitle, context=args.context,
                                         changes_only=args.changes_only)
    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "annotated-diff.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    for s in skipped:
        warn(f"not shown: {s.label}: {s.reason}")
    ambiguous = [n for n in placed if n.warning]
    for n in ambiguous:
        warn(f"note #{n.num} ({n.source}): anchor {n.anchor!r} in {n.file}: {n.warning}")
    for n in problems:
        target = f"anchor {n.anchor!r} in {n.file}" if n.anchor else f"file {n.file!r}"
        warn(f"note #{n.num} ({n.source}): {target}: {n.problem} — shown under 'Notes not tied to a line'")
    print(f"wrote {out.resolve()} ({len(files)} files, {len(placed)} notes on lines or files, "
          f"{len(problems)} unplaced, {len(skipped)} files not shown)")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    if args.strict and (problems or ambiguous):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
