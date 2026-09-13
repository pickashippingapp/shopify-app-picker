#!/usr/bin/env python3
"""Per-app review digests via the Claude API (Message Batches).

  run.py prepare            build data/digest_requests.jsonl for apps above the review floor (no network)
  run.py one <handle>       run a single app synchronously (streaming) -> digests/<handle>.json  (spot-check)
  run.py submit             create the batch from digest_requests.jsonl -> data/batch_state.json
  run.py status             print batch progress
  run.py collect            fetch results -> digests/<handle>.json, validate every cited id and quote

  run.py cli <handle>       same digest through the local `claude -p` CLI (Claude Code subscription, no API key)
  run.py cli-all [N]        every app above the floor through the CLI, N at a time (default 2); skips existing digests
  run.py revalidate         re-run the citation/quote validator over every digest on disk (no network)

Credentials for the API path: ANTHROPIC_API_KEY from the environment, or a KEY=VALUE line in ./.env (gitignored).
The CLI path uses whatever `claude` is logged in as.
"""
import json, os, re, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA, OUT = ROOT / "data", ROOT / "digests"
MODEL = "claude-opus-5"
FLOOR = 30
MAX_TOKENS = 16000

SEGMENTS = ["multi_carrier_labels_rates", "checkout_rates_rules", "order_tracking", "returns_exchanges",
            "fulfilment_3pl", "single_carrier", "regional_aggregator", "local_delivery_pickup", "other"]

SYSTEM = """You are a shipping-operations engineer who has integrated a dozen carriers with Shopify and now helps merchants choose apps. You will receive every App Store review of one Shopify shipping app, plus precomputed statistics. Produce a digest that a store owner can act on and that a sceptic can verify.

Rules, in priority order:
1. Every theme, red flag and claim cites review ids that actually say that. Never cite an id you have not read. Counts are your honest tally of reviews expressing the theme, not the number of ids you list (list up to 5 representative ids per theme; pick recent ones first).
2. Quotes are verbatim substrings of the cited review's text. Shorten with an ellipsis only between complete phrases; never paraphrase inside quotation marks.
3. Weight recent reviews more. A failure last reported in 2023 on an app with 500 reviews since is "resolved" or "fading", not a current risk; say when it was last seen.
4. Negative and 4-star "but…" reviews carry most of the information. Rating-only reviews (no text) carry none beyond the number.
5. Do not rank on stars. Judge what the app is for, who it serves well, who it fails, and what the developer does when it fails.
6. Review integrity: if a large share of reviews come from stores using the app for hours or days, or read as solicited during onboarding chat, say so plainly and lower your confidence.
7. Do not invent features, prices or carriers the reviews do not mention. If reviews are too thin to judge something, say so.
8. Plain language for a store owner. No marketing adjectives. Name carriers, countries, channels and order volumes when the reviews name them."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "handle": {"type": "string"},
        "segment": {"type": "string", "enum": SEGMENTS},
        "segment_note": {"type": "string", "description": "One sentence: what the app actually does, per the reviews."},
        "one_liner": {"type": "string", "description": "The verdict in one sentence a store owner would repeat."},
        "right_for": {"type": "array", "items": {"type": "string"}, "description": "Store profiles this app serves well, evidenced by reviews."},
        "wrong_for": {"type": "array", "items": {"type": "string"}, "description": "Store profiles that will be disappointed, evidenced by reviews."},
        "good_at": {"type": "array", "items": {"$ref": "#/$defs/theme"}},
        "failures": {"type": "array", "items": {"$ref": "#/$defs/failure"}},
        "red_flags": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"flag": {"type": "string"}, "review_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["flag", "review_ids"]},
            "description": "Billing surprises, data loss, silent breakage, support silence, lock-in. Empty if none."},
        "fit_signals": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "volumes": {"type": "string", "description": "Order volumes reviewers mention, or 'not stated'."},
                "countries": {"type": "array", "items": {"type": "string"}},
                "carriers": {"type": "array", "items": {"type": "string"}},
                "channels": {"type": "array", "items": {"type": "string"}, "description": "Marketplaces/platforms beyond Shopify that reviewers ship from."}},
            "required": ["volumes", "countries", "carriers", "channels"]},
        "developer_response": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "pattern": {"type": "string", "description": "How the developer replies to negative reviews and whether issues get fixed, with ids."},
                "resolves": {"type": "string", "enum": ["usually", "sometimes", "rarely", "no_replies"]},
                "review_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["pattern", "resolves", "review_ids"]},
        "review_integrity": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "note": {"type": "string"},
                "solicited_share": {"type": "string", "enum": ["none", "some", "heavy"]}},
            "required": ["note", "solicited_share"]},
        "trend_note": {"type": "string", "description": "How the last 12 months compare with the years before: quality, volume, themes."},
        "verdict": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "description": "3-5 sentences for a store owner deciding whether to install."},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "confidence_reason": {"type": "string"}},
            "required": ["summary", "confidence", "confidence_reason"]},
    },
    "required": ["handle", "segment", "segment_note", "one_liner", "right_for", "wrong_for", "good_at", "failures",
                 "red_flags", "fit_signals", "developer_response", "review_integrity", "trend_note", "verdict"],
    "$defs": {
        "theme": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "theme": {"type": "string"},
                "count": {"type": "integer", "description": "Reviews expressing this theme."},
                "review_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "quote": {"type": "object", "additionalProperties": False,
                          "properties": {"review_id": {"type": "string"}, "text": {"type": "string"}},
                          "required": ["review_id", "text"]}},
            "required": ["theme", "count", "review_ids", "quote"]},
        "failure": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "theme": {"type": "string"},
                "count": {"type": "integer"},
                "review_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "quote": {"type": "object", "additionalProperties": False,
                          "properties": {"review_id": {"type": "string"}, "text": {"type": "string"}},
                          "required": ["review_id", "text"]},
                "trend": {"type": "string", "enum": ["rising", "steady", "fading", "resolved"]},
                "last_seen": {"type": "string", "description": "YYYY-MM of the most recent review expressing it."}},
            "required": ["theme", "count", "review_ids", "quote", "trend", "last_seen"]},
    },
}


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"'))


def client():
    load_env()
    import anthropic
    return anthropic.Anthropic()


def load_data():
    apps = {a["handle"]: a for a in map(json.loads, (DATA / "apps.jsonl").open())}
    stats = {s["handle"]: s for s in map(json.loads, (DATA / "stats.jsonl").open())}
    reviews = collections.defaultdict(list)
    for l in (DATA / "reviews.jsonl").open():
        r = json.loads(l); reviews[r["app"]].append(r)
    return apps, stats, reviews


def review_line(r):
    s = f"[{r['id']}] {r['date']} | {r['rating']}★ | {r['country'] or '?'} | {r['tenure'] or '?'}"
    if r["edited"]:
        s += " | edited"
    s += f"\n{r['body']}" if r["body"] else "\n(no text)"
    if r["reply"]:
        s += f"\n  ↳ developer replied {r['reply_date']}: {r['reply']}"
    return s


def build_params(handle, apps, stats, reviews):
    a, s = apps[handle], stats[handle]
    rows = sorted(reviews[handle], key=lambda r: r["id"], reverse=True)  # newest first
    body = (
        f"# App: {a['name']} (handle: {handle})\n"
        f"Listing: rating {a['rating']}, {a['review_count']} reviews, pricing: {a['pricing']}, "
        f"built for Shopify: {a['built_for_shopify']}\nTagline: {a['tagline']}\n\n"
        f"# Precomputed statistics (trust these numbers)\n{json.dumps(s, ensure_ascii=False)}\n\n"
        f"# All {len(rows)} reviews, newest first\n\n" + "\n\n".join(review_line(r) for r in rows)
    )
    return {
        "model": MODEL, "max_tokens": MAX_TOKENS, "system": SYSTEM,
        "messages": [{"role": "user", "content": body}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def prepare():
    apps, stats, reviews = load_data()
    handles = [h for h, s in stats.items() if s["reviews"] >= FLOOR]
    with (DATA / "digest_requests.jsonl").open("w") as f:
        for h in handles:
            p = build_params(h, apps, stats, reviews)
            f.write(json.dumps({"custom_id": h, "params": p}, ensure_ascii=False) + "\n")
    chars = sum(len(json.dumps(build_params(h, apps, stats, reviews)["messages"][0]["content"])) for h in handles)
    print(f"{len(handles)} requests -> {DATA / 'digest_requests.jsonl'}; ~{chars // 4:,} input tokens (chars/4)")


def validate(handle, digest, reviews):
    """Every cited id must be one of the app's reviews; every quote must be a substring of that review."""
    by_id = {r["id"]: r for r in reviews[handle]}
    fold = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2011": "-", "\u2013": "-", "\u2014": "-", "\u00a0": " "})
    norm = lambda t: re.sub(r"\s+", " ", t.translate(fold)).strip().lower()
    problems, cited = [], set()

    def check_ids(ids, where):
        for i in ids:
            cited.add(i)
            if i not in by_id:
                problems.append(f"{where}: unknown review id {i}")

    def check_quote(q, where):
        if q["review_id"] not in by_id:
            problems.append(f"{where}: quote cites unknown id {q['review_id']}"); return
        hay = norm(by_id[q["review_id"]]["body"] + " " + (by_id[q["review_id"]]["reply"] or ""))
        parts = [norm(p) for p in re.split(r"\s*(?:\.\.\.|…)\s*", q["text"]) if p.strip()]
        if not all(p in hay for p in parts):
            problems.append(f"{where}: quote not verbatim in {q['review_id']}: {q['text'][:80]!r}")

    for sec in ("good_at", "failures"):
        for t in digest.get(sec, []):
            check_ids(t["review_ids"], f"{sec}/{t['theme']}"); check_quote(t["quote"], f"{sec}/{t['theme']}")
            if t["count"] > len(by_id):
                problems.append(f"{sec}/{t['theme']}: count {t['count']} exceeds review total")
    for rf in digest.get("red_flags", []):
        check_ids(rf["review_ids"], f"red_flag/{rf['flag']}")
    check_ids(digest.get("developer_response", {}).get("review_ids", []), "developer_response")
    return {"cited_ids": len(cited), "problems": problems}


def write_digest(handle, msg, reviews, usage=None):
    text = next(b.text for b in msg.content if b.type == "text")
    digest = json.loads(text)
    digest["_validation"] = validate(handle, digest, reviews)
    digest["_meta"] = {"model": msg.model, "stop_reason": msg.stop_reason,
                       "usage": usage or msg.usage.to_dict() if hasattr(msg.usage, "to_dict") else dict(msg.usage)}
    OUT.mkdir(exist_ok=True)
    (OUT / f"{handle}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    return digest


def one(handle):
    apps, stats, reviews = load_data()
    c = client()
    params = build_params(handle, apps, stats, reviews)
    with c.messages.stream(**params) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason != "end_turn":
        print(f"stop_reason={msg.stop_reason}", file=sys.stderr)
    d = write_digest(handle, msg, reviews)
    u = msg.usage
    print(f"{handle}: in={u.input_tokens} out={u.output_tokens} cited={d['_validation']['cited_ids']} "
          f"problems={len(d['_validation']['problems'])} -> {OUT / (handle + '.json')}")
    for p in d["_validation"]["problems"]:
        print("  !", p)


def submit():
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    reqs = [json.loads(l) for l in (DATA / "digest_requests.jsonl").open()]
    done = {p.stem for p in OUT.glob("*.json")} if OUT.exists() else set()
    reqs = [r for r in reqs if r["custom_id"] not in done]
    if not reqs:
        print("nothing to submit; all digests exist"); return
    c = client()
    batch = c.messages.batches.create(requests=[
        Request(custom_id=r["custom_id"], params=MessageCreateParamsNonStreaming(**r["params"])) for r in reqs])
    (DATA / "batch_state.json").write_text(json.dumps({"batch_id": batch.id, "submitted": len(reqs)}))
    print(f"batch {batch.id}: {len(reqs)} requests, status {batch.processing_status}")


def status():
    st = json.loads((DATA / "batch_state.json").read_text())
    b = client().messages.batches.retrieve(st["batch_id"])
    print(b.id, b.processing_status, b.request_counts)


def collect():
    _, _, reviews = load_data()
    st = json.loads((DATA / "batch_state.json").read_text())
    c = client()
    b = c.messages.batches.retrieve(st["batch_id"])
    if b.processing_status != "ended":
        print(f"not finished: {b.processing_status} {b.request_counts}"); return
    ok, bad, problems = 0, [], 0
    for res in c.messages.batches.results(st["batch_id"]):
        h = res.custom_id
        if res.result.type == "succeeded":
            msg = res.result.message
            if msg.stop_reason not in ("end_turn", "stop_sequence"):
                bad.append((h, f"stop_reason={msg.stop_reason}")); continue
            try:
                d = write_digest(h, msg, reviews); ok += 1; problems += len(d["_validation"]["problems"])
            except Exception as e:  # malformed JSON despite schema, or unexpected shape
                bad.append((h, f"{type(e).__name__}: {e}"))
        else:
            err = getattr(res.result, "error", None)
            bad.append((h, f"{res.result.type}: {getattr(err, 'type', '')} {getattr(err, 'message', '')}"))
    print(f"digests written: {ok}; failed: {len(bad)}; citation problems across all: {problems}")
    for h, why in bad:
        print("  x", h, why)
    (DATA / "batch_collect_report.json").write_text(json.dumps({"ok": ok, "failed": bad, "problems": problems}, indent=2))


class UsageLimit(RuntimeError):
    """The CLI refused because of a usage window, rate limit or auth problem: stop the run, don't burn the queue."""


def cli_one(handle, apps, stats, reviews, model="opus"):
    """Run one digest through `claude -p`. Prompt on stdin, schema-constrained JSON back."""
    import subprocess, tempfile
    params = build_params(handle, apps, stats, reviews)
    # no --bare (it skips credential resolution); no --max-turns (the schema answer arrives as a 2nd turn via a tool call)
    cmd = ["claude", "-p", "--no-session-persistence", "--model", model, "--effort", "high",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--system-prompt", SYSTEM, "--tools", ""]
    # cwd=/tmp so the CLI does not load this repo's CLAUDE.md or project memory into every call
    proc = subprocess.run(cmd, input=params["messages"][0]["content"], capture_output=True, text=True, timeout=3600, cwd="/tmp")
    try:
        env = json.loads(proc.stdout)
    except ValueError:
        env = None
    if proc.returncode != 0 or not env or env.get("is_error"):
        msg = (env or {}).get("result") if env else proc.stdout[-300:]
        text = f"claude exited {proc.returncode}: {str(msg)[:300]} {proc.stderr[-200:]}".strip()
        if re.search(r"safeguards flagged|can't respond to this message", text, re.I) and model == "opus":
            print(f"  {handle}: Opus safeguard false positive, retrying on sonnet", file=sys.stderr, flush=True)
            return cli_one(handle, apps, stats, reviews, model="sonnet")
        if re.search(r"limit|rate|quota|too many|overloaded|resets at|not logged in", text, re.I):
            raise UsageLimit(text)
        raise RuntimeError(text)
    digest = env.get("structured_output")
    if digest is None:  # fall back to parsing the result text as JSON
        digest = json.loads(env["result"])
    digest["_validation"] = validate(handle, digest, reviews)
    digest["_meta"] = {"backend": "claude-cli", "model": model, "models_used": list((env.get("modelUsage") or {}).keys()),
                       "fallback": model != "opus", "cost_usd": env.get("total_cost_usd"),
                       "duration_ms": env.get("duration_ms"), "usage": env.get("usage"), "session_id": env.get("session_id")}
    OUT.mkdir(exist_ok=True)
    (OUT / f"{handle}.json").write_text(json.dumps(digest, ensure_ascii=False, indent=2))
    return digest


def cli(handle):
    apps, stats, reviews = load_data()
    d = cli_one(handle, apps, stats, reviews)
    v = d["_validation"]
    print(f"{handle}: cited={v['cited_ids']} problems={len(v['problems'])} cost=${d['_meta']['cost_usd']} "
          f"{(d['_meta']['duration_ms'] or 0) // 1000}s -> {OUT / (handle + '.json')}")
    for p in v["problems"]:
        print("  !", p)


def cli_all(parallel=2):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    apps, stats, reviews = load_data()
    done = {p.stem for p in OUT.glob("*.json")} if OUT.exists() else set()
    handles = [h for h, s in sorted(stats.items(), key=lambda kv: kv[1]["reviews"]) if s["reviews"] >= FLOOR and h not in done]
    print(f"{len(handles)} apps to digest, {parallel} at a time", flush=True)
    stop = False

    def work(h):
        if stop:
            return None
        return cli_one(h, apps, stats, reviews)

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = {ex.submit(work, h): h for h in handles}
        for f in as_completed(futs):
            h = futs[f]
            try:
                d = f.result()
                if d is None:
                    continue
                v = d["_validation"]
                print(f"ok  {h}: cited={v['cited_ids']} problems={len(v['problems'])} ${d['_meta']['cost_usd']}", flush=True)
            except UsageLimit as e:
                stop = True
                print(f"STOP {h}: {str(e)[:300]}", flush=True)
            except Exception as e:
                print(f"ERR {h}: {type(e).__name__}: {str(e)[:300]}", flush=True)
    if stop:
        print("run stopped on a usage limit; re-run cli-all when the window resets", flush=True)


def revalidate():
    _, _, reviews = load_data()
    total = 0
    for f in sorted(OUT.glob("*.json")):
        d = json.loads(f.read_text())
        d["_validation"] = validate(d["handle"], d, reviews)
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2))
        total += len(d["_validation"]["problems"])
        for p in d["_validation"]["problems"]:
            print(f"{d['handle']}: {p}")
    print(f"revalidated {len(list(OUT.glob('*.json')))} digests, {total} problems")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    {"prepare": prepare, "one": lambda: one(sys.argv[2]), "submit": submit, "status": status, "collect": collect,
     "cli": lambda: cli(sys.argv[2]), "cli-all": lambda: cli_all(int(sys.argv[2]) if len(sys.argv) > 2 else 2),
     "revalidate": revalidate}.get(
        cmd, lambda: sys.exit(__doc__))()
