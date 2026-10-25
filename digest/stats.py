#!/usr/bin/env python3
"""Deterministic per-app statistics from data/reviews.jsonl -> data/stats.jsonl.

These are the numbers the comparison table shows and the digest prompt is given as
context. They are computed here, never by the model: momentum, negative share,
recency, developer reply behaviour, country and tenure mix.
"""
import json, re, sys, collections
from datetime import date, timedelta
from pathlib import Path

DATA = Path("data")  # relative to the working directory: run from the project root
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                      "August", "September", "October", "November", "December"], 1)}


def parse_date(s):
    m = re.match(r"(\w+) (\d+), (\d{4})", s or "")
    return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))) if m else None


def app_stats(rows, today):
    y1 = today - timedelta(days=365)
    y2 = today - timedelta(days=730)
    dated = [(parse_date(r["date"]), r) for r in rows]
    last12 = [r for d, r in dated if d and d > y1]
    prior12 = [r for d, r in dated if d and y2 < d <= y1]
    neg = [r for r in rows if r["rating"] and r["rating"] <= 2]
    neg12 = [r for r in last12 if r["rating"] and r["rating"] <= 2]
    first = min((d for d, _ in dated if d), default=None)
    months_live = max(1, (today - first).days / 30.4) if first else None
    return {
        "reviews": len(rows),
        "avg_rating": round(sum(r["rating"] for r in rows) / len(rows), 2),
        "negative": len(neg),
        "negative_share": round(len(neg) / len(rows), 3),
        "reviews_last_12m": len(last12),
        "reviews_prior_12m": len(prior12),
        "momentum_per_month": round(len(last12) / 12, 1),
        "negative_share_last_12m": round(len(neg12) / len(last12), 3) if last12 else None,
        "first_review": first.isoformat() if first else None,
        "months_on_store": round(months_live) if months_live else None,
        "reply_rate_all": round(sum(1 for r in rows if r["reply"]) / len(rows), 2),
        "reply_rate_negative": round(sum(1 for r in neg if r["reply"]) / len(neg), 2) if neg else None,
        "with_text": sum(1 for r in rows if r["body"]),
        "countries": collections.Counter(r["country"] for r in rows if r["country"]).most_common(8),
        "tenure_under_month_share": round(sum(1 for r in rows if r["tenure"] and re.search(r"\b(hour|minute|day)s?\b", r["tenure"])) / len(rows), 2),
        "as_of": today.isoformat(),  # the date the windows above are measured from; site/build.py prints it as the corpus month
    }


def main():
    """Write one row per app in reviews.jsonl; the previous stats.jsonl is replaced only once every row is computed.

    An app in reviews.jsonl but not in apps.jsonl keeps its row, with "name": null, and is named in a warning on
    stderr. reviews.jsonl is the corpus; the gap almost always means apps.jsonl is older than the reviews (apps.jsonl
    is committed, reviews.jsonl is local, so a checkout of an earlier commit pairs an old listing with newer reviews).
    """
    today = date.today()
    by_app = collections.defaultdict(list)
    for l in (DATA / "reviews.jsonl").open():
        r = json.loads(l); by_app[r["app"]].append(r)
    apps = {a["handle"]: a for a in map(json.loads, (DATA / "apps.jsonl").open())}
    out = []
    for h, rows in sorted(by_app.items(), key=lambda kv: -len(kv[1])):
        s = {"handle": h, "name": apps[h]["name"] if h in apps else None, **app_stats(rows, today)}
        out.append(json.dumps(s, ensure_ascii=False) + "\n")
    missing = [h for h in by_app if h not in apps]
    if missing:
        print(f"warning: {len(missing)} apps in reviews.jsonl are not in apps.jsonl (stats kept, name null): "
              f"{', '.join(missing)}", file=sys.stderr)
    tmp = DATA / "stats.jsonl.tmp"
    tmp.write_text("".join(out), encoding="utf-8")
    tmp.replace(DATA / "stats.jsonl")
    print(f"stats for {len(by_app)} apps -> {DATA / 'stats.jsonl'}")


if __name__ == "__main__":
    main()
