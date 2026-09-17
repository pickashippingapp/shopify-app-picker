#!/usr/bin/env python3
"""Scrape a Shopify App Store category and every review of its apps into JSONL.

Usage:
  scrape.py listing [category]   -> data/apps.jsonl   (one row per app card; category is the slug from the
                                    apps.shopify.com/categories/<slug> URL, default: shipping solutions)
  scrape.py reviews [--min N]    -> data/reviews.jsonl (every review of every app; resumable)
  scrape.py reparse              -> rebuild data/reviews.jsonl from the raw HTML cache, no network
  scrape.py refresh [--min N]    -> re-pull: moves the current dataset to data/reviews.prev.jsonl, fetches the listing
                                    and every review again, then diffs the two (see `diff`). Resumable through
                                    data/refresh_in_progress; a finished refresh removes it and reviews_done.txt
  scrape.py diff                 -> compare data/reviews.prev.jsonl with data/reviews.jsonl, no network: append one
                                    event per new, removed, re-rated, edited or re-replied review (with the whole
                                    previous record) to data/review_changes.jsonl and print a summary. Re-rated 1-2 star reviews are listed, since a merchant
                                    revising a bad review after the developer got in touch is a signal in itself

Every fetched reviews page is kept gzipped under data/raw/<handle>/p<N>.html.gz so parser
fixes never cost a refetch.

Stdlib only. Run from the project root: data/ is resolved from the working directory. Polite: one request per DELAY seconds, browser UA, retries with backoff.
Resumable: reviews for a handle are written only when all its pages succeeded, and
handles already present in data/reviews_done.txt are skipped on the next run.
"""
import collections, gzip, json, re, sys, time, urllib.request, urllib.error, html
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://apps.shopify.com"
CATEGORY = "orders-and-shipping-shipping-solutions-shipping"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
DELAY = 1.0
DATA = Path("data")  # relative to the working directory: run from the project root
RAW = DATA / "raw"


def get(url, tries=5):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            time.sleep(DELAY)
            return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            wait = 5 * (attempt + 1) if e.code in (429, 503) else 2
            print(f"  http {e.code} on {url}, retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:  # network blips, and the minutes after a laptop wakes before Wi-Fi is back
            last = e
            wait = 15 * 2 ** attempt  # 15 s .. 4 min, about 8 minutes in total
            print(f"  {type(e).__name__} on {url}, retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"gave up on {url}: {last!r}")


class Blocks(HTMLParser):
    """Collect ordered text tokens (plus a few attribute markers) inside every element
    that carries `marker_attr`. Skips script/style/svg."""

    def __init__(self, marker_attr):
        super().__init__()
        self.marker_attr = marker_attr
        self.blocks = []      # list of (attrs, tokens)
        self.stack = []       # depth counters for open blocks
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "svg"):
            self.skip += 1
        if self.marker_attr in a:
            self.blocks.append((a, []))
            self.stack.append(1)
        elif self.stack:
            self.stack[-1] += 1
        if self.stack and not self.skip:
            toks = self.blocks[-1][1]
            if "aria-label" in a and tag not in ("svg", "path"):
                toks.append(("aria", a["aria-label"]))
            if tag == "a" and "href" in a:
                toks.append(("href", a["href"]))
            if "title" in a and a["title"] != "Copy link to review":
                toks.append(("title", a["title"]))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg"):
            self.skip = max(0, self.skip - 1)
        if self.stack:
            self.stack[-1] -= 1
            if self.stack[-1] == 0:
                self.stack.pop()

    def handle_data(self, data):
        if self.stack and not self.skip:
            d = " ".join(data.split())
            if d:
                self.blocks[-1][1].append(("text", html.unescape(d)))


# ---------- listing ----------

def parse_cards(page):
    p = Blocks("data-app-card-handle-value")
    p.feed(page)
    rows = []
    for attrs, toks in p.blocks:
        text = [t for k, t in toks if k == "text"]
        joined = " ".join(text)
        m = re.search(r"([0-9.]+) out of 5 stars", joined)
        n = re.search(r"([0-9,]+) total reviews", joined)
        price = None
        for t in text:
            if re.match(r"(Free|From \$|\$[0-9]|Free plan|Free trial|Free to install)", t):
                price = t
                break
        rows.append({
            "handle": attrs["data-app-card-handle-value"],
            "name": attrs.get("data-app-card-name-value"),
            "icon": attrs.get("data-app-card-icon-url-value"),
            "rating": float(m.group(1)) if m else None,
            "review_count": int(n.group(1).replace(",", "")) if n else 0,
            "pricing": price,
            "built_for_shopify": "Built for Shopify" in joined,
            "tagline": next((t for t in text if len(t) > 25 and "reviews" not in t and "stars" not in t and t != attrs.get("data-app-card-name-value")), None),
            "text": joined,
        })
    return rows


def listing(category):
    out = DATA / "apps.jsonl"
    seen, rows, page = set(), [], 1
    while True:
        body = get(f"{BASE}/categories/{category}/all?page={page}")
        cards = parse_cards(body) if body else []
        new = [c for c in cards if c["handle"] not in seen]
        print(f"page {page}: {len(cards)} cards, {len(new)} new", file=sys.stderr)
        if not new:
            break
        for c in new:
            seen.add(c["handle"]); c["category"] = category; c["listing_page"] = page
            rows.append(c)
        page += 1
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} apps -> {out}", file=sys.stderr)


# ---------- reviews ----------

DATE = re.compile(r"^(Edited )?(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$")


def parse_reviews(page, handle):
    p = Blocks("data-review-content-id")
    p.feed(page)
    out = []
    for attrs, toks in p.blocks:
        rating = next((int(v[0]) for k, v in toks if k == "aria" and "out of 5 stars" in v), None)
        store = next((v for k, v in toks if k == "title"), None)
        # the store name token is the first text token after the title attribute; a body that ends with
        # the store's signature would otherwise be taken as the anchor and shift country/tenure by one
        text, store_i, seen_title = [], None, False
        for k, t in toks:
            if k == "title":
                seen_title = True
            elif k == "text" and t not in ("Show more", "Show less"):
                if seen_title and store_i is None and t == store:
                    store_i = len(text)
                text.append(t)
        # order observed: [Edited] date, body..., store, country, tenure, "<Dev> replied <date>", reply...
        row = {"id": attrs["data-review-content-id"], "app": handle, "rating": rating,
               "date": None, "edited": False, "body": "", "store": store, "country": None, "tenure": None,
               "reply_date": None, "reply": None}
        i = 0
        if text and DATE.match(text[0]):
            row["edited"] = text[0].startswith("Edited ")
            row["date"] = text[0].replace("Edited ", ""); i = 1
        if store_i is None:  # fall back to the tenure token as the anchor
            t_i = next((j for j, t in enumerate(text) if "using the app" in t), None)
            store_i = t_i - 2 if t_i is not None and t_i >= i + 2 else None
            if store_i is not None:
                row["store"] = text[store_i]
        if store_i is not None:
            row["body"] = " ".join(text[i:store_i])
            tail = text[store_i + 1:]
            if tail:
                row["country"] = tail[0]; tail = tail[1:]
            if tail and "using the app" in tail[0]:
                row["tenure"] = tail[0]; tail = tail[1:]
        else:
            row["body"] = " ".join(text[i:]); tail = []
        if tail:
            m = re.match(r"(.+) replied (.+ \d{4})$", tail[0])
            if m:
                row["reply_date"] = m.group(2); row["reply"] = " ".join(tail[1:])
        out.append(row)
    agg = re.search(r'"aggregateRating":\{[^}]*"ratingValue":([0-9.]+),"ratingCount":(\d+)', page)
    return out, (float(agg.group(1)), int(agg.group(2))) if agg else (None, None)


def reviews(min_reviews):
    apps = [json.loads(l) for l in (DATA / "apps.jsonl").open()]
    done_f = DATA / "reviews_done.txt"
    done = set(done_f.read_text().split()) if done_f.exists() else set()
    out = (DATA / "reviews.jsonl").open("a")
    meta = (DATA / "apps_rating.jsonl").open("a")
    todo = [a for a in apps if a["review_count"] >= min_reviews and a["handle"] not in done]
    print(f"{len(todo)} apps to fetch ({sum(a['review_count'] for a in todo)} reviews)", file=sys.stderr)
    for a in todo:
        h, rows, page = a["handle"], [], 1
        agg = (None, None)
        (RAW / h).mkdir(parents=True, exist_ok=True)
        while True:
            body = get(f"{BASE}/{h}/reviews?page={page}")
            if body is None:
                break
            with gzip.open(RAW / h / f"p{page}.html.gz", "wt", encoding="utf-8") as g:
                g.write(body)
            got, agg2 = parse_reviews(body, h)
            if agg2[0] is not None:
                agg = agg2
            if not got:
                break
            rows.extend(got); page += 1
        for r in rows:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
        meta.write(json.dumps({"handle": h, "rating": agg[0], "rating_count": agg[1], "fetched": len(rows)}) + "\n")
        out.flush(); meta.flush()
        with done_f.open("a") as f:
            f.write(h + "\n")
        print(f"{h}: {len(rows)} reviews / {a['review_count']} listed", file=sys.stderr)


def reparse():
    """Rebuild reviews.jsonl and apps_rating.jsonl from data/raw without touching the network."""
    n = 0
    with (DATA / "reviews.jsonl").open("w") as out, (DATA / "apps_rating.jsonl").open("w") as meta:
        for d in sorted(RAW.iterdir()):
            rows, agg = [], (None, None)
            for f in sorted(d.glob("p*.html.gz"), key=lambda f: int(f.stem[1:-5])):
                with gzip.open(f, "rt", encoding="utf-8") as g:
                    got, agg2 = parse_reviews(g.read(), d.name)
                if agg2[0] is not None:
                    agg = agg2
                rows.extend(got)
            for r in rows:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            meta.write(json.dumps({"handle": d.name, "rating": agg[0], "rating_count": agg[1], "fetched": len(rows)}) + "\n")
            n += len(rows)
    print(f"reparsed {n} reviews", file=sys.stderr)


def refresh(min_reviews):
    """Full re-pull of listing and reviews, then a diff against the previous dataset.

    A full pull rather than an incremental one because reviews get unpublished (Shopify's 2026 sweep) and edited, and
    neither shows up if you stop at the first known id. About 4,000 requests for the shipping category, ~70 minutes.
    If reviews_done.txt exists a previous refresh was interrupted: resume it instead of rotating again."""
    cur, prev, done_f = DATA / "reviews.jsonl", DATA / "reviews.prev.jsonl", DATA / "reviews_done.txt"
    marker = DATA / "refresh_in_progress"  # exists only between rotation and completion; reviews_done.txt alone is not
    if marker.exists():                    # enough, a finished first `reviews` run leaves that file behind too
        print("resuming an interrupted refresh", file=sys.stderr)
    else:
        if cur.exists():
            cur.replace(prev)
        rating = DATA / "apps_rating.jsonl"
        if rating.exists():
            rating.replace(DATA / "apps_rating.prev.jsonl")
        done_f.unlink(missing_ok=True)
        marker.touch()
        listing(CATEGORY)
    reviews(min_reviews)
    done_f.unlink(missing_ok=True)
    marker.unlink()
    diff()


def diff():
    """Compare the previous pull with the current one and append every change to data/review_changes.jsonl.

    Event kinds: new, removed (unpublished by Shopify or the reviewer), rerated (stars changed), edited (text or
    date changed, stars the same), reply (only the developer's reply was added, changed or deleted). A rerated event
    from 1-2 stars upward is the case worth reading: it usually means the developer reached the merchant after the
    review, which the digests treat as evidence of what happens when things break.

    `old` is the whole previous record (store, country, tenure, full body, reply text), because after the next
    rotation this log is the only place it exists. `new` is a short snapshot: the full record is in reviews.jsonl,
    and lands here as `old` if it changes again."""
    def load(path):
        return {r["id"]: r for r in map(json.loads, path.open())} if path.exists() else {}
    old, new = load(DATA / "reviews.prev.jsonl"), load(DATA / "reviews.jsonl")
    if not old:
        print(f"{len(new)} reviews, no previous pull to compare with", file=sys.stderr)
        return
    today = time.strftime("%Y-%m-%d")
    snap = lambda r: {"rating": r["rating"], "date": r["date"], "body": r["body"][:300], "reply": bool(r.get("reply"))}
    events = []
    for i, r in new.items():
        o = old.get(i)
        if o is None:
            events.append({"seen": today, "app": r["app"], "id": i, "kind": "new", "new": snap(r)})
        elif r["rating"] != o["rating"]:
            events.append({"seen": today, "app": r["app"], "id": i, "kind": "rerated", "old": o, "new": snap(r)})
        elif (r["body"], r["date"]) != (o["body"], o["date"]):
            events.append({"seen": today, "app": r["app"], "id": i, "kind": "edited", "old": o, "new": snap(r)})
        elif (r.get("reply"), r.get("reply_date")) != (o.get("reply"), o.get("reply_date")):
            events.append({"seen": today, "app": r["app"], "id": i, "kind": "reply", "old": o, "new": snap(r)})
    for i, o in old.items():
        if i not in new:
            events.append({"seen": today, "app": o["app"], "id": i, "kind": "removed", "old": o})
    with (DATA / "review_changes.jsonl").open("a") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    kinds = collections.Counter(e["kind"] for e in events)
    up = [e for e in events if e["kind"] == "rerated" and e["old"]["rating"] <= 2 and e["new"]["rating"] > e["old"]["rating"]]
    old_apps, new_apps = {r["app"] for r in old.values()}, {r["app"] for r in new.values()}
    print(f"refresh done: {len(new)} reviews ({len(old)} before); +{kinds['new']} new, -{kinds['removed']} removed, "
          f"{kinds['rerated']} re-rated ({len(up)} up from 1-2 stars), {kinds['edited']} edited, "
          f"{kinds['reply']} replies changed; "
          f"apps: +{len(new_apps - old_apps)} -{len(old_apps - new_apps)}", file=sys.stderr)
    for e in up:
        print(f"  revised up  {e['app']} {e['id']}: {e['old']['rating']}->{e['new']['rating']} stars, "
              f"{'developer replied' if e['new']['reply'] else 'no reply'}; was: {e['old']['body'][:90]!r}", file=sys.stderr)
    for kind in ("new", "removed"):
        for app, n in collections.Counter(e["app"] for e in events if e["kind"] == kind).most_common(8):
            print(f"  {kind:8} {app}: {n}", file=sys.stderr)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "listing"
    if cmd == "listing":
        listing(sys.argv[2] if len(sys.argv) > 2 else CATEGORY)
    elif cmd == "reviews":
        n = int(sys.argv[sys.argv.index("--min") + 1]) if "--min" in sys.argv else 1
        reviews(n)
    elif cmd == "reparse":
        reparse()
    elif cmd == "refresh":
        n = int(sys.argv[sys.argv.index("--min") + 1]) if "--min" in sys.argv else 1
        refresh(n)
    elif cmd == "diff":
        diff()
    else:
        sys.exit(__doc__)
