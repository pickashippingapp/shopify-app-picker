"""Tests for run.py on an app with a stats.jsonl row but no apps.jsonl row: synthetic data in a temp directory.

run.py binds DATA and OUT from the working directory at import, so each test repoints them (a chdir would not, and
prepare() would overwrite the real data/digest_requests.jsonl). No network: cli_one and subprocess.run are stubbed.
Run from the repo root: python3 -m unittest discover -s pipeline/digest -p 'test_*.py'
"""
import contextlib, io, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run

WARN = "warning: 1 apps to digest are not in apps.jsonl (digested without listing details): ghost\n"

LISTED = {"handle": "listed", "name": "Listed App", "rating": 4.5, "review_count": 40, "pricing": "Free",
          "built_for_shopify": True, "tagline": "Labels fast"}


def review(app, rid):
    return {"app": app, "id": rid, "rating": 5, "date": "October 1, 2026", "body": "works well", "reply": None,
            "country": "Australia", "tenure": "2 years using the app", "store": "s", "edited": False, "reply_date": None}


def stat(handle, n, name=None):
    return {"handle": handle, "name": name, "reviews": n, "as_of": "2026-11-13"}


class UnlistedAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = run.DATA, run.OUT
        run.DATA, run.OUT = Path(self.tmp.name, "data"), Path(self.tmp.name, "digests")
        run.DATA.mkdir()

    def tearDown(self):
        run.DATA, run.OUT = self.saved
        self.tmp.cleanup()

    def write(self, name, rows):
        (run.DATA / name).write_text("".join(json.dumps(r) + "\n" for r in rows))

    def corpus(self, counts, apps=(LISTED,)):
        """counts: {handle: number of reviews}; stats rows follow, names from apps where listed."""
        names = {a["handle"]: a["name"] for a in apps}
        self.write("apps.jsonl", list(apps))
        self.write("stats.jsonl", [stat(h, n, names.get(h)) for h, n in counts.items()])
        self.write("reviews.jsonl", [review(h, i) for h, n in counts.items() for i in range(n)])

    def test_build_params_unlisted_app_has_no_listing_and_no_none(self):
        stats = {"ghost": stat("ghost", 1)}
        p = run.build_params("ghost", {}, stats, {"ghost": [review("ghost", 1)]})
        head = p["messages"][0]["content"].split("# Precomputed")[0]
        self.assertEqual(head, "# App: ghost (handle: ghost)\nListing details (rating, pricing, tagline) are not available.\n\n")
        self.assertNotIn("None", head)

    def test_build_params_listed_app_header_unchanged(self):
        p = run.build_params("listed", {"listed": LISTED}, {"listed": stat("listed", 1, "Listed App")},
                             {"listed": [review("listed", 1)]})
        head = p["messages"][0]["content"].split("# Precomputed")[0]
        self.assertEqual(head, "# App: Listed App (handle: listed)\n"
                               "Listing: rating 4.5, 40 reviews, pricing: Free, built for Shopify: True\n"
                               "Tagline: Labels fast\n\n")

    def test_prepare_writes_unlisted_floor_app_and_warns(self):
        self.corpus({"listed": run.FLOOR, "ghost": run.FLOOR, "small": run.FLOOR - 1})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            run.prepare()
        ids = [json.loads(l)["custom_id"] for l in (run.DATA / "digest_requests.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(ids), ["ghost", "listed"])
        self.assertIn(WARN, err.getvalue())  # names ghost only: not "small" (below the floor)

    def test_cli_all_reaches_unlisted_app_and_warns(self):
        self.corpus({"listed": run.FLOOR, "ghost": run.FLOOR, "done": run.FLOOR})
        run.OUT.mkdir()
        (run.OUT / "done.json").write_text("{}")  # unlisted but already digested: skipped, as for any app
        called = []
        stub = lambda h, *a, **k: called.append(h) or {"_validation": {"cited_ids": 0, "problems": []}, "_meta": {"cost_usd": 0}}
        err = io.StringIO()
        with mock.patch.object(run, "cli_one", stub), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            run.cli_all(1)
        self.assertEqual(sorted(called), ["ghost", "listed"])
        self.assertIn(WARN, err.getvalue())  # names ghost only: not "done" (already digested)

    def test_cli_digests_unlisted_app_end_to_end(self):
        self.corpus({"ghost": 2})
        digest = {"handle": "ghost", "good_at": [], "failures": [], "red_flags": [], "developer_response": {"review_ids": [1]}}
        env = {"structured_output": digest, "total_cost_usd": 0, "duration_ms": 0, "modelUsage": {}}
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(env), stderr="")
        err = io.StringIO()
        with mock.patch("subprocess.run", return_value=fake) as sp, contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            run.cli("ghost")
        self.assertIn("Listing details (rating, pricing, tagline) are not available.", sp.call_args.kwargs["input"])
        written = json.loads((run.OUT / "ghost.json").read_text())
        self.assertEqual(written["_validation"], {"cited_ids": 1, "problems": []})
        self.assertIn(WARN, err.getvalue())


if __name__ == "__main__":
    unittest.main()
