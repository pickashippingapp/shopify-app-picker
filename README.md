# shopify-app-picker

The pipeline behind [pickashippingapp.com](https://pickashippingapp.com): scrape a Shopify App Store
category and every review of every app in it, compute per-app statistics, and have Claude write a
digest per app in which every claim cites review ids and every quote is checked against the review text.

Three scripts, no framework:

    scraper/scrape.py   listing + reviews scraper. Stdlib only, one request per second, resumable,
                        keeps the raw HTML so parser fixes never cost a refetch.
    digest/stats.py     deterministic per-app numbers (momentum, negative share, reply rate, tenure mix).
                        Computed here, never by the model.
    digest/run.py       one Claude call per app above a review floor -> digests/<handle>.json.
                        Fixed JSON schema; a validator rejects any cited id that is not one of the app's
                        reviews and any quote that is not a verbatim substring of the cited review.

## Scrape

    python3 scraper/scrape.py listing                 # shipping category (default), ~45 pages -> data/apps.jsonl
    python3 scraper/scrape.py listing <category-slug> # any category: the slug from apps.shopify.com/categories/<slug>
    python3 scraper/scrape.py reviews --min 30        # every review of every app with >= 30 reviews -> data/reviews.jsonl
    python3 scraper/scrape.py reparse                 # rebuild reviews.jsonl from data/raw/, no network

`reviews` is resumable: an app is written only when all its pages succeeded, and apps listed in
`data/reviews_done.txt` are skipped on the next run. Each review row carries id, rating, date, body,
store, country, tenure and the developer's reply. Review ids link back to the review on
apps.shopify.com, which is what makes the digests checkable.

## Digest

    python3 digest/stats.py                 # data/reviews.jsonl -> data/stats.jsonl
    python3 digest/run.py cli <handle>      # one app through the Claude Code CLI (`claude -p`, no API key)
    python3 digest/run.py cli-all 2         # every app above the floor, two at a time; skips existing digests
    python3 digest/run.py revalidate        # re-run the citation and quote checks over digests/

With an API key (`ANTHROPIC_API_KEY`, or a line in `.env`), `pip install anthropic` and use the
Message Batches path instead: `prepare`, `submit`, `status`, `collect`. Same prompt, same schema,
same validator, about a seventh of the cost.

The system prompt and schema are written for shipping apps (the segment list, the persona). Change
`SEGMENTS` and `SYSTEM` in `digest/run.py` for another category.

## Rules the pipeline enforces

- Every theme, red flag and developer-behaviour claim carries review ids. The validator fails a digest
  that cites an id the app does not have.
- Quotes are verbatim. The validator normalises whitespace and curly quotes, then requires each
  ellipsis-separated fragment to be a substring of the cited review or its developer reply.
- Counts, not adjectives: the model is told to tally, and the statistics it is given are computed in
  `stats.py`, not estimated.
- Recent is separated from historical: every failure theme carries a trend and a last-seen month.
- Polite scraping: one request per second, browser user agent, retries with backoff. Keep the raw
  pages local; do not redistribute review text.

## Licence

MIT.
