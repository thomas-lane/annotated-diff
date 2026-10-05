"""Tests for annotated_diff.py. Run: python3 -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/annotated-diff"
SCRIPT = SKILL_DIR / "scripts/annotated_diff.py"
_spec = importlib.util.spec_from_file_location("annotated_diff", SCRIPT)
ad = importlib.util.module_from_spec(_spec)
sys.modules["annotated_diff"] = ad  # dataclasses look the module up while the class is created
_spec.loader.exec_module(ad)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ad.main(argv)
    return code, out.getvalue(), err.getvalue()


BEFORE = "a\nb\nc\nd\n"
AFTER = "a\nB\nc\nd\ne\n"


class DiffRows(unittest.TestCase):
    def test_whole_file_with_both_line_numbers(self):
        rows = ad.diff_rows(BEFORE, AFTER)
        self.assertEqual(rows, [
            ("ctx", 1, 1, "a"),
            ("del", 2, None, "b"),
            ("add", None, 2, "B"),
            ("ctx", 3, 3, "c"),
            ("ctx", 4, 4, "d"),
            ("add", None, 5, "e"),
        ])

    def test_added_and_deleted_files(self):
        self.assertEqual([r[0] for r in ad.diff_rows("", "x\ny\n")], ["add", "add"])
        self.assertEqual([r[0] for r in ad.diff_rows("x\ny\n", "")], ["del", "del"])
        self.assertEqual(ad.diff_rows("", ""), [])


class Anchors(unittest.TestCase):
    rows = ad.diff_rows("keep\nold value = 1\nsame\n", "keep\nnew value = 2\nsame\n-flag\n-flag twice\n")

    def test_plain_anchor_matches_an_added_line(self):
        row, count, problem = ad.match_anchor(self.rows, "value = 2")
        self.assertEqual((self.rows[row][0], self.rows[row][3], count, problem),
                         ("add", "new value = 2", 1, ""))

    def test_minus_prefix_matches_a_removed_line(self):
        row, _, _ = ad.match_anchor(self.rows, "-old value")
        self.assertEqual(self.rows[row][:2], ("del", 2))

    def test_plus_prefix_forces_an_added_line(self):
        row, count, _ = ad.match_anchor(self.rows, "+-flag twice")
        self.assertEqual((self.rows[row][3], count), ("-flag twice", 1))

    def test_unmatched_anchor_explains_why(self):
        self.assertIsNone(ad.match_anchor(self.rows, "nowhere")[0])
        _, _, problem = ad.match_anchor(self.rows, "old value")
        self.assertIn('prefix the anchor with "-"', problem)
        _, _, problem = ad.match_anchor(self.rows, "same")
        self.assertIn("unchanged line", problem)
        self.assertEqual(ad.match_anchor(self.rows, "")[2], "empty anchor")
        self.assertIn("several lines", ad.match_anchor(self.rows, "a\nb")[2])

    def test_ambiguous_anchor_uses_first_and_reports_count(self):
        row, count, _ = ad.match_anchor(self.rows, "+-flag")
        self.assertEqual((self.rows[row][3], count), ("-flag", 2))

    def test_whitespace_insensitive_fallback(self):
        row, _, _ = ad.match_anchor(self.rows, "new   value =  2")
        self.assertEqual(self.rows[row][3], "new value = 2")

    def test_place_notes_numbers_in_reading_order(self):
        files = [ad.FileDiff("f.txt", BEFORE, AFTER)]
        rows = [ad.diff_rows(BEFORE, AFTER)]
        notes = [ad.Note(text="second", file="f.txt", anchor="e", order=0),
                 ad.Note(text="first", file="f.txt", anchor="B", order=1),
                 ad.Note(text="file-level", file="f.txt", order=2),
                 ad.Note(text="lost", file="f.txt", anchor="zzz", order=3),
                 ad.Note(text="other", file="g.txt", anchor="B", order=4)]
        placed, unplaced = ad.place_notes(files, rows, notes)
        self.assertEqual([n.text for n in placed], ["file-level", "first", "second"])
        self.assertEqual({n.text: n.problem for n in unplaced},
                         {"lost": "no added line contains it", "other": "file is not in the diff"})

    def test_renamed_file_accepts_old_or_new_name(self):
        f = ad.FileDiff("new.txt", "x\n", "y\n", "renamed", "old.txt")
        rows = [ad.diff_rows(f.before, f.after)]
        for name in ("new.txt", "old.txt", "./new.txt"):
            placed, unplaced = ad.place_notes([f], rows, [ad.Note(text="n", file=name, anchor="y")])
            self.assertEqual((len(placed), unplaced), (1, []), name)


class NotesFiles(unittest.TestCase):
    def test_prototype_format_with_rule_field(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "n.json")
            p.write_text(json.dumps({
                "comments": [{"file": "a", "anchor": "x", "rule": "7", "note": "why"}],
                "general": [{"title": "T", "note": "N"}, "plain"]}))
            comments, general = ad.load_notes([str(p)])
        self.assertEqual((comments[0].label, comments[0].text, comments[0].anchor), ("7", "why", "x"))
        self.assertEqual([(g.title, g.text) for g in general], [("T", "N"), ("", "plain")])

    def test_invalid_json_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "bad.json")
            p.write_text("{nope")
            with self.assertRaises(ad.UsageError):
                ad.load_notes([str(p)])


class Rendering(unittest.TestCase):
    EVIL = '<script>alert("x")</script>'

    def render(self, **kw):
        files = [ad.FileDiff(f"evil{self.EVIL}.html", "a\n", f"a\n{self.EVIL}\n")]
        comments = [ad.Note(text=f"note {self.EVIL} `<b>code</b>` **bold**", label=f'"><img src=x>',
                            file=files[0].label, anchor="alert", title=self.EVIL),
                    ad.Note(text="unplaced " + self.EVIL, file=files[0].label, anchor=f"no {self.EVIL}")]
        general = [ad.Note(text=self.EVIL, title=self.EVIL)]
        page, placed, problems = ad.render_page(files, [ad.Skipped(self.EVIL, self.EVIL)], comments,
                                                general, title=self.EVIL, subtitle=self.EVIL, **kw)
        return page, placed, problems

    def test_everything_is_escaped(self):
        page, placed, problems = self.render()
        self.assertEqual((len(placed), len(problems)), (1, 1))
        body = page.split("<script>", 1)[0] + page.rsplit("</script>", 1)[1]
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>code</b>", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", body)
        self.assertIn("<code>&lt;b&gt;code&lt;/b&gt;</code>", page)
        self.assertIn("<b>bold</b>", page)
        self.assertEqual(page.count("<script>"), 1)  # only the page's own script

    def test_self_contained(self):
        page, _, _ = self.render()
        self.assertIsNone(re.search(r'(src|href)="(https?:)?//', page))
        self.assertNotIn("@import", page)

    def test_unplaced_notes_go_to_the_general_panel(self):
        page, _, problems = self.render()
        self.assertIn("Notes not tied to a line (2)", page)
        self.assertIn("line not found", page)
        self.assertEqual(problems[0].num, 3)  # after the placed note and the general note

    def test_margin_and_inline_cards_and_pins(self):
        page, placed, _ = self.render()
        n = placed[0].num
        self.assertIn(f'id="c-{n}"', page)
        self.assertIn(f'id="ci-{n}"', page)
        self.assertIn(f'class="pin" href="#c-{n}"', page)
        self.assertIn(f'data-anchor="{n}"', page)

    def test_changes_only_flag_and_context(self):
        page, _, _ = self.render(changes_only=True, context=5)
        self.assertIn('<body class="changes-only" data-context="5">', page)
        self.assertIn('id="only" checked', page)

    def test_sticky_file_header_follows_measured_top_bar(self):
        page, _, _ = self.render()
        css = re.search(r"\.file-head \{([^}]*)\}", page).group(1)
        self.assertIn("position:sticky", css)
        self.assertIn("top:var(--top-h", css)
        self.assertNotRegex(css, r"top:\s*\d+px")
        self.assertIn("setProperty('--top-h'", page)
        self.assertIn("new ResizeObserver(syncTop).observe(topBar)", page)
        self.assertIn("window.addEventListener('resize', syncTop)", page)

    def test_fixed_column_widths_do_not_depend_on_first_row(self):
        page, _, _ = self.render()
        self.assertIn('<colgroup><col class="cnum"><col class="cnum"><col class="csign"><col></colgroup>', page)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        (self.dir / "old.txt").write_text(BEFORE)
        (self.dir / "new.txt").write_text(AFTER)

    def notes(self, comments):
        p = self.dir / "notes.json"
        p.write_text(json.dumps({"comments": comments}))
        return str(p)

    def test_file_mode_and_strict(self):
        out = self.dir / "page.html"
        base = ["--pair", "f.txt", str(self.dir / "old.txt"), str(self.dir / "new.txt"),
                "--pair", "added.txt", "/dev/null", str(self.dir / "new.txt"), "--out", str(out)]
        good = self.notes([{"file": "f.txt", "anchor": "-b", "note": "removed b"}])
        code, stdout, err = run_main(base + ["--notes", good, "--strict"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 files", stdout)
        self.assertIn("badge added", out.read_text())
        bad = self.notes([{"file": "f.txt", "anchor": "missing text", "note": "x"}])
        code, _, err = run_main(base + ["--notes", bad])
        self.assertEqual(code, 0)
        self.assertIn("'missing text'", err)
        self.assertEqual(run_main(base + ["--notes", bad, "--strict"])[0], 1)

    def test_bad_input_exits_2(self):
        code, _, err = run_main(["--pair", "x", str(self.dir / "nope"), str(self.dir / "new.txt"),
                                 "--out", str(self.dir / "o.html")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class GitMode(unittest.TestCase):
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.repo / "mod.txt").write_text(BEFORE)
        (self.repo / "gone.txt").write_text("bye\n")
        (self.repo / "moved.txt").write_text("".join(f"line {i}\n" for i in range(20)))
        (self.repo / "img.bin").write_bytes(b"\x00\x01")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def test_working_tree_changes(self):
        (self.repo / "mod.txt").write_text(AFTER)
        (self.repo / "gone.txt").unlink()
        self.git("mv", "moved.txt", "renamed.txt")
        (self.repo / "img.bin").write_bytes(b"\x00\x02")
        (self.repo / "fresh.txt").write_text("brand new\n")
        files, skipped, desc = ad.collect_git(str(self.repo), "HEAD", None, [], [], True, False,
                                              ad.DEFAULT_MAX_BYTES)
        got = {f.label: (f.status, f.old_label) for f in files}
        self.assertEqual(got, {"mod.txt": ("modified", None), "gone.txt": ("deleted", None),
                               "renamed.txt": ("renamed", "moved.txt"), "fresh.txt": ("added", None)})
        self.assertEqual([(s.label, s.reason) for s in skipped], [("img.bin", "binary file")])
        self.assertTrue(desc.endswith("→ working tree"))

        files, _, _ = ad.collect_git(str(self.repo), "HEAD", None, [], [], False, False,
                                     ad.DEFAULT_MAX_BYTES)
        self.assertNotIn("fresh.txt", {f.label for f in files})
        files, _, _ = ad.collect_git(str(self.repo), "HEAD", None, ["mod.txt"], [], True, False,
                                     ad.DEFAULT_MAX_BYTES)
        self.assertEqual([f.label for f in files], ["mod.txt"])

    def test_commit_range_and_unknown_ref(self):
        (self.repo / "mod.txt").write_text(AFTER)
        self.git("commit", "-qam", "change")
        files, _, desc = ad.collect_git(str(self.repo), "HEAD~1", "HEAD", [], [], True, False,
                                        ad.DEFAULT_MAX_BYTES)
        self.assertEqual([(f.label, f.after) for f in files], [("mod.txt", AFTER)])
        with self.assertRaises(ad.UsageError):
            ad.collect_git(str(self.repo), "no-such-ref", None, [], [], True, False, 1000)


class SkillPackaging(unittest.TestCase):
    """The skill must load unchanged in Claude Code, Codex and Pi (Agent Skills format)."""

    SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}

    def frontmatter(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
        self.assertIsNotNone(m, "SKILL.md must start with YAML frontmatter")
        fields = dict(re.findall(r"^([A-Za-z][\w-]*):[ \t]*(.*)$", m.group(1), re.M))
        return fields, m.group(2)

    def test_frontmatter_follows_the_agent_skills_spec(self):
        fields, _ = self.frontmatter()
        self.assertLessEqual(set(fields), self.SPEC_FIELDS)
        self.assertEqual(fields["name"], SKILL_DIR.name)
        self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertTrue(0 < len(fields["description"]) <= 1024)

    def test_instructions_use_no_harness_specific_variables(self):
        _, body = self.frontmatter()
        self.assertNotIn("${", body)
        self.assertNotRegex(body, r"\$(CLAUDE|CODEX|PI)_")
        self.assertIn("scripts/annotated_diff.py", body)
        self.assertTrue(SCRIPT.is_file())

    def test_plugin_marketplace_points_at_the_repository_root(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        (entry,) = market["plugins"]
        self.assertEqual((entry["name"], entry["source"]), (plugin["name"], "./"))
        self.assertTrue((ROOT / "skills" / plugin["name"] / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
