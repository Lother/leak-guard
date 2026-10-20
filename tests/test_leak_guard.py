import base64
import gzip
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import leak_guard as lg  # noqa: E402

NEEDLE = "octocat"


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.m = lg.Matcher(NEEDLE)

    def test_three_casings_hit(self):
        for text in (b"octocat", b"Octocat", b"OCTOCAT"):
            self.assertEqual(self.m.count(b"user: " + text), 1, text)

    def test_odd_casing_is_ignored(self):
        self.assertEqual(self.m.count(b"OctoCat"), 0)

    def test_mid_word_is_ignored(self):
        self.assertEqual(self.m.count(b"myoctocat"), 0)

    def test_word_start_after_punctuation_hits(self):
        self.assertEqual(self.m.count(b"/home/octocat/x"), 1)
        self.assertEqual(self.m.count(b"octocat_tools"), 1)

    def test_utf16le(self):
        self.assertEqual(self.m.count("path\\Octocat\\a".encode("utf-16-le")), 1)

    def test_base64(self):
        self.assertGreaterEqual(self.m.count(base64.b64encode(b"octocat:secret")), 1)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.s = lg.Scanner([NEEDLE, "hubber"])

    def test_multiple_needles(self):
        self.assertEqual(self.s.count(b"octocat and hubber"), 2)

    def test_nested_zip(self):
        inner = make_zip({"cfg.json": b'{"owner": "octocat"}'})
        outer = make_zip({"readme.txt": b"clean", "inner.zip": inner})
        hits, members = self.s.count_deep(outer)
        self.assertGreaterEqual(hits, 1)  # stored zips also expose the raw bytes at each level
        self.assertIn("inner.zip!cfg.json", members)

    def test_gzip(self):
        hits, members = self.s.count_deep(gzip.compress(b"by Hubber"))
        self.assertEqual(hits, 1)
        self.assertEqual(members, ["<gzip>"])

    def test_clean_archive(self):
        self.assertEqual(self.s.count_deep(make_zip({"a.txt": b"nothing here"}))[0], 0)


class NeedleFileTests(unittest.TestCase):
    def write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_comments_and_case(self):
        self.assertEqual(lg.load_needles(self.write("# mine\nOctocat\n\nhubber # work\n")), ["octocat", "hubber"])

    def test_missing_file_fails_closed(self):
        with self.assertRaises(lg.ConfigError):
            lg.load_needles("/nonexistent/needles.txt")

    def test_empty_file_fails_closed(self):
        with self.assertRaises(lg.ConfigError):
            lg.load_needles(self.write("# only comments\n"))

    def test_bad_needle_message_hides_value(self):
        with self.assertRaises(lg.ConfigError) as cm:
            lg.load_needles(self.write("zq\n"))
        self.assertNotIn("zq", str(cm.exception))

    def test_cli_exit_codes(self):
        needles = self.write("octocat\n")
        clean, dirty = self.write("hello"), self.write("Octocat was here")
        self.assertEqual(lg.main(["--needle-file", needles, "files", clean]), lg.EXIT_CLEAN)
        self.assertEqual(lg.main(["--needle-file", needles, "files", dirty]), lg.EXIT_BLOCKED)
        self.assertEqual(lg.main(["--needle-file", "/nonexistent", "files", clean]), lg.EXIT_ERROR)


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class GitModeTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.remote = os.path.join(root.name, "remote.git")
        self.repo = os.path.join(root.name, "work")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
            git(self.repo, "config", k, v)
        git(self.repo, "remote", "add", "origin", self.remote)
        self.commit("a.txt", "first")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")

    def commit(self, name, text, message="c"):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", message)

    def test_only_outgoing_objects_are_scanned(self):
        self.assertEqual(lg.outgoing_objects(self.repo, "HEAD"), [])
        self.commit("b.txt", "second")
        self.assertEqual(len(lg.outgoing_objects(self.repo, "HEAD")), 3)  # commit, tree, blob

    def test_blocks_needle_in_new_blob(self):
        self.commit("b.txt", "contact: octocat")
        ok, findings = lg.scan_git(self.repo, "HEAD", [NEEDLE])
        self.assertFalse(ok)
        self.assertTrue(any(f.startswith("blob") for f in findings))

    def test_blocks_needle_in_annotated_tag(self):
        self.commit("b.txt", "clean")
        git(self.repo, "tag", "-a", "v1", "-m", "released by Octocat")
        ok, findings = lg.scan_git(self.repo, "v1", [NEEDLE])
        self.assertFalse(ok)
        self.assertTrue(any(f.startswith("tag") for f in findings))

    def test_clean_push_passes(self):
        self.commit("b.txt", "clean")
        self.assertEqual(lg.scan_git(self.repo, "HEAD", [NEEDLE]), (True, []))


if __name__ == "__main__":
    unittest.main()
