# Automotive eBay Listings Dashboard

Polls eBay search results on a schedule, filters them with hard rules,
flags statistical price outliers, sends only those outliers to a local
Ollama model for a price/scam-risk opinion, and shows everything in a
local web dashboard. No eBay API, no purchasing/watching - read-only,
link-out to the real listing.

## How it fits together

```
main.py                    - runs the scrape loop + dashboard together
config.json                - global settings (timing, rules, ollama, dashboard)
search_targets.json         - what to search for (edit this to add real searches)
modules/ebot.py             - httpx + bs4 eBay scraper
modules/rules.py            - price/keyword hard filters + outlier detection (z-score)
modules/ollama_audit.py     - sends only outliers to Ollama for a verdict
modules/db.py                - SQLite storage (aiosqlite)
modules/dashboard.py         - FastAPI app
templates/index.html         - the dashboard page
data/listings.db             - created automatically on first run
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If you want the Ollama audit step working:

```bash
# https://ollama.com - install, then:
ollama pull llama3.2:3b   # or whatever model you set in config.json
ollama serve              # if not already running as a service
```

## Configure your searches

Edit `search_targets.json`. Each entry:

```json
{
  "name": "unique-id-for-this-search",
  "enabled": true,
  "query": "the eBay search string",
  "price_min": 100,
  "price_max": 3000,
  "include_keywords": [],
  "exclude_keywords": ["core", "for parts"],
  "max_pages": 2
}
```

The two example entries are `enabled: false` - flip to `true` or add
your own. `search_targets.json` is re-read at the start of every poll
cycle, so you can edit it while `main.py` is running and changes pick
up on the next cycle.

`config.json` holds the shared settings - polling interval (default:
random 15-30 min between full cycles, with a random few-second delay
between individual page/target requests), the outlier z-score
threshold, and the Ollama model/host.

## Run it

```bash
python main.py
```

You'll get a menu:

```
1) Start polling + host dashboard (full pipeline)
2) Host dashboard only (view existing data, no scraping)
3) Run Ollama audit pass (audit any flagged-but-unaudited outliers, then exit)
4) Exit
```

Ctrl+C during modes 1 or 2 stops just that mode and drops you back at
the menu (not the whole program) - pick another option, or "4" to
actually quit. Mode 3 finishes on its own and also returns you to the
menu. Ctrl+C at the menu prompt itself exits normally.

Or skip the menu for scripts/services with `--mode`:

```bash
python main.py --mode full        # scrape + dashboard together
python main.py --mode dashboard   # dashboard only, no scraping
python main.py --mode ollama      # one-shot: audit any outliers missing a verdict, then exit
```

Mode 3 is useful if you turned Ollama off for a while, or a target ran
before Ollama was reachable - it catches up any outlier that's missing
a verdict without re-scraping or re-touching listings that already
have one.

Once running in mode 1 or 2, open http://127.0.0.1:8000/ - filter by
search target, verdict, or "outliers only". The page auto-refreshes
every 60 seconds.

## Notes / known limitations

- **Outlier stats exclude the listing itself** (fixed): a listing's own
  price is never part of the mean/stdev used to judge it -
  `db.get_price_stats()` takes an `exclude_item_id`, and `main.py`
  always passes the current listing's ID. Without this, a genuine
  outlier waters down its own baseline and becomes harder to detect.
- **Ollama only (re)audits on new information** (fixed): a flagged
  listing gets audited once, and again only if its price changes -
  not every single poll cycle it remains listed. See `needs_audit` in
  `process_listing()` in `main.py` if you want to change that logic
  (e.g. re-audit after N days regardless of price).

- **eBay's search endpoint is bot-guarded, more so than item pages.**
  In testing, a plain request to `/sch/i.html` got silently redirected
  to a generic "Shop by Category" page instead of real results - a
  `200 OK` that isn't actually search results. `ebot.py` now warms up
  with a homepage visit first (to pick up cookies), keeps one
  consistent browser identity per session instead of rotating per
  request, and sends a fuller realistic header set + Referer. If a
  request still gets redirected away from search or returns HTML with
  zero parseable listings, the raw HTML is automatically saved to
  `debug_html/` with a filename indicating why (`redirected` vs
  `empty_parse`) - check there first if listings stop showing up.
- **If it's still getting blocked after that**: some anti-bot systems
  fingerprint the TLS handshake itself (JA3), which no header changes
  can spoof from plain httpx. The next escalation - still short of a
  full browser - is the `curl_cffi` library, which impersonates a real
  browser's TLS fingerprint while keeping a requests-like API. Worth
  trying only if the debug_html dumps confirm you're still getting
  decoy/blocked pages after the current fix.
- **eBay markup changes**: `modules/ebot.py` parses eBay's search
  result HTML with specific CSS class names (`s-item`, etc). eBay
  tweaks this periodically - the `empty_parse` debug dumps are the
  first place to check if requests are succeeding but nothing parses.
- **Outlier detection needs history**: the z-score check only kicks in
  once a search target has `outlier_min_samples` (default 5) prices
  logged, so Ollama audits won't start on the very first cycle for a
  brand-new target.
- **Be a polite scraper**: the built-in delays and jitter are meant to
  keep this from looking like a hammering bot, but there's no
  guarantee against IP-based rate limiting - if you see a lot of 429s,
  widen the delay ranges in `config.json`.
- **Telegram alerts** were intentionally left out of this pass per
  your call - the dashboard is standalone for now. If you want alerts
  later, `process_listing()` in `main.py` is the natural hook point
  (fire a message whenever `is_new` and/or `outlier` is true).
