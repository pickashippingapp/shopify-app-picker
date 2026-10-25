"""Tests for stats.py main(): synthetic data in a temp directory, no real data.

Run from the repo root: python3 -m unittest discover -s pipeline/digest -p 'test_*.py'
"""
import contextlib, io, json, os, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stats


def review(app, rid, rating=5):
    return {"app": app, "id": rid, "rating": rating, "date": "October 1, 2026", "body": "ok", "reply": None,
            "country": "Australia", "tenure": "2 years using the app", "store": "s", "edited": False, "reply_date": None}


class MainTest(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        Path("data").mkdir()

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def write(self, name, rows):
        Path("data", name).write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_app_missing_from_apps_jsonl_is_kept_and_named(self):
        self.write("apps.jsonl", [{"handle": "listed", "name": "Listed App"}])
        self.write("reviews.jsonl", [review("listed", 1), review("listed", 2), review("unlisted", 3)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            stats.main()
        rows = {r["handle"]: r for r in map(json.loads, Path("data/stats.jsonl").read_text().splitlines())}
        self.assertEqual(set(rows), {"listed", "unlisted"})
        self.assertEqual(rows["listed"]["name"], "Listed App")
        self.assertIsNone(rows["unlisted"]["name"])
        self.assertEqual(rows["unlisted"]["reviews"], 1)
        self.assertIn("warning: 1 apps", err.getvalue())
        self.assertIn("unlisted", err.getvalue())

    def test_failure_inside_the_loop_leaves_previous_stats_intact(self):
        Path("data/stats.jsonl").write_text('{"handle": "old", "as_of": "2026-10-13"}\n')
        before = Path("data/stats.jsonl").read_bytes()
        self.write("apps.jsonl", [{"handle": "good", "name": "Good"}, {"handle": "bad", "name": "Bad"}])
        # "good" sorts first (more reviews), so the old code had written its row before "bad" raised
        self.write("reviews.jsonl", [review("good", 1), review("good", 2), review("bad", 3, rating="1")])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(TypeError):
            stats.main()
        self.assertEqual(Path("data/stats.jsonl").read_bytes(), before)
        self.assertFalse(Path("data/stats.jsonl.tmp").exists())


if __name__ == "__main__":
    unittest.main()
