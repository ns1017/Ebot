"""
eBay search scraper.

Uses eBay's public search results page (https://www.ebay.com/sch/i.html).
No eBay API, no login, no purchasing/watching - read-only.

IMPORTANT: eBay's search endpoint is more heavily bot-guarded than
individual item pages. A "successful" 200 response can still be a
decoy page (e.g. redirected to a generic category page) rather than
real results. This module treats that as distinct from "genuinely
zero results" and dumps the raw HTML to modules/../debug_html/ so you
can inspect exactly what came back instead of guessing.

Countermeasures used here (no browser automation required):
  - One consistent browser "identity" (UA + matching sec-ch-ua headers)
    per search session, instead of rotating per-request (rotating mid
    session is itself a bot signal).
  - A homepage visit before hitting the search endpoint, so any
    anti-bot cookie/token gets set the way a real visit would set it.
  - A Referer header on search requests.
  - Jittered delays between requests (configured in config.json).

If eBay keeps redirecting/blocking despite this, the likely next step
is TLS-fingerprint spoofing (e.g. the `curl_cffi` library's browser
"impersonate" mode) since some anti-bot systems key off the TLS
handshake itself, which no amount of header tweaking can fix. That's
a bigger change (new dependency) so it's not applied automatically -
check the debug_html dumps first to see what's actually happening.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import urllib.parse as urlparse
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.ebay.com/sch/i.html"
HOME_URL = "https://www.ebay.com/"
ITEM_ID_RE = re.compile(r"/itm/(\d+)")
PRICE_RE = re.compile(r"[\d,]+\.\d{2}|\d[\d,]*")

# eBay's known fixed condition vocabulary - used to pick the condition
# out of the card's subtitle lines, which can also contain shipping/
# guarantee blurbs in no fixed order.
CONDITION_VALUES = {
    "new", "new with tags", "new without tags", "new with defects",
    "certified - refurbished", "excellent - refurbished", "very good - refurbished",
    "good - refurbished", "seller refurbished", "open box", "pre-owned", "used",
    "for parts or not working", "brand new",
}

DEBUG_DIR = Path(__file__).parent.parent / "debug_html"

log = logging.getLogger("ebot")

# Paired UA + matching sec-ch-ua so the header set is internally
# consistent (a Chrome UA with no sec-ch-ua headers at all, or
# mismatched platform hints, is itself a tell).
BROWSER_PROFILES = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"',
        "platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"',
        "platform": '"Linux"',
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "sec_ch_ua": None,
        "platform": '"macOS"',
    },
]


def build_search_url(query: str, price_min: Optional[float], price_max: Optional[float], page: int) -> str:
    params = {
        "_nkw": query,
        "_pgn": str(page),
        "_sacat": "0",
    }
    if price_min:
        params["_udlo"] = str(price_min)
    if price_max:
        params["_udhi"] = str(price_max)
    return f"{BASE_URL}?{urlparse.urlencode(params)}"


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    match = PRICE_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def parse_listings(html: str) -> list[dict]:
    """
    Parses eBay search-result cards. eBay's current (2026) layout uses
    `li.s-card` with a `data-listingid` attribute carrying the item ID
    directly - much more reliable than pulling it out of the href's
    tracking-param-laden query string. `li.s-item` (the older layout)
    is kept as a fallback in case eBay serves it to some sessions.

    A hidden template/skeleton card ships in every page (used by their
    JS for client-side cloning) with title "Shop on eBay" - explicitly
    filtered out below rather than relied upon to just fail selection.
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    cards = soup.select("li.s-card")
    layout = "s-card"
    if not cards:
        cards = soup.select("li.s-item")
        layout = "s-item"

    for card in cards:
        if layout == "s-card":
            listing_id = card.get("data-listingid")
            title_el = card.select_one(".s-card__title span")
            price_el = card.select_one(".s-card__price")
            link_el = card.select_one("a.s-card__link[href]")
            image_el = card.select_one("img.s-card__image")
            subtitle_els = card.select(".s-card__subtitle span")
        else:
            listing_id = None
            title_el = card.select_one("div.s-item__title span") or card.select_one("div.s-item__title")
            price_el = card.select_one("span.s-item__price")
            link_el = card.select_one("a.s-item__link")
            image_el = card.select_one("img.s-item__image-img")
            subtitle_els = card.select("span.s-item__subtitle")

        if not title_el or not link_el:
            continue  # most likely the hidden skeleton/template card

        # A few card variants apparently put a badge label ("New Listing",
        # "Sponsored", etc.) in the same slot our selector grabs for the
        # title, instead of the actual product title. Rather than silently
        # store garbage, skip these - a title-less listing is better logged
        # as skipped than saved as fake data. If this fires a lot, the fix
        # is a more specific title selector, which needs a fresh HTML
        # sample of one of these cards to get right.

        title = title_el.get_text(strip=True)
        BADGE_LABELS = {"shop on ebay", "new listing", "sponsored", "sponsored listing"}
        if not title or title.lower() in BADGE_LABELS:
            if title:
                log.debug(f"Skipped a card whose title slot held a badge label ('{title}'), not a real title.")
            continue

        href = link_el.get("href", "")
        if not listing_id:
            id_match = ITEM_ID_RE.search(href)
            listing_id = id_match.group(1) if id_match else None
        if not listing_id:
            continue

        price = _parse_price(price_el.get_text(strip=True) if price_el else "")

        condition = None
        for sub in subtitle_els:
            text = sub.get_text(strip=True)
            if text.lower() in CONDITION_VALUES:
                condition = text
                break

        results.append({
            "item_id": listing_id,
            "title": title,
            "price": price,
            "currency": "USD",
            "condition": condition,
            "url": href.split("?")[0],
            "image_url": image_el.get("src") if image_el else None,
            # Not present in the current search-card markup at all
            # (eBay dropped it from SRP cards) - left as None rather
            # than guessed at.
            "location": None,
        })

    return results


def _session_headers(profile: dict) -> dict:
    headers = {
        "User-Agent": profile["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # Deliberately not requesting "br" (Brotli) - httpx can only
        # decode it if the optional brotli package is installed, and
        # requesting an encoding you can't decode causes silent
        # garbage/empty bodies rather than a clear error.
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if profile.get("sec_ch_ua"):
        headers["sec-ch-ua"] = profile["sec_ch_ua"]
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = profile["platform"]
    return headers


async def _get(client: httpx.AsyncClient, url: str, config: dict, referer: Optional[str] = None) -> Optional[httpx.Response]:
    max_retries = config["scraper"]["max_retries"]
    timeout = config["scraper"]["request_timeout_seconds"]
    headers = {}
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                await asyncio.sleep(random.uniform(5, 15) * attempt)
                continue
            resp.raise_for_status()
        except httpx.HTTPError:
            if attempt == max_retries:
                return None
            await asyncio.sleep(random.uniform(2, 6) * attempt)
    return None


def _dump_debug_html(target_name: str, page: int, html: str, reason: str) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        fname = DEBUG_DIR / f"{target_name}_p{page}_{reason}_{int(time.time())}.html"
        fname.write_text(html, encoding="utf-8", errors="ignore")
        log.warning(f"Saved debug HTML to {fname} (reason: {reason}) - inspect this to see what eBay actually returned.")
    except OSError:
        pass


async def search_target(target: dict, config: dict) -> list[dict]:
    """Runs the search for one target across its configured page count."""
    scraper_cfg = config["scraper"]
    max_pages = target.get("max_pages", scraper_cfg.get("max_pages_per_target", 2))
    delay_min = scraper_cfg["delay_between_requests_min_seconds"]
    delay_max = scraper_cfg["delay_between_requests_max_seconds"]

    profile = random.choice(BROWSER_PROFILES)
    headers = _session_headers(profile)

    all_listings: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(http2=True, headers=headers, follow_redirects=True) as client:
        # Warm up like a real visit: load the homepage first so any
        # anti-bot cookie/token gets set the way it would for an actual
        # browser, before we ever touch the search endpoint.
        home_resp = await _get(client, HOME_URL, config)
        if home_resp is None:
            log.warning(f"[{target['name']}] could not load ebay.com homepage at all - network issue?")
            return []
        await asyncio.sleep(random.uniform(1.5, 3.5))

        referer = HOME_URL
        for page in range(1, max_pages + 1):
            url = build_search_url(target["query"], target.get("price_min"), target.get("price_max"), page)
            resp = await _get(client, url, config, referer=referer)
            if resp is None:
                break

            html = resp.text
            final_url = str(resp.url)

            # A 200 that lands somewhere other than the search results
            # (e.g. redirected to a generic category page) means we got
            # served a decoy, not "zero results" - worth flagging loudly
            # and saving, rather than silently logging "fetched 0".
            if "_nkw=" not in final_url:
                log.warning(
                    f"[{target['name']}] page {page} was redirected away from search results to "
                    f"{final_url} - this usually means bot-detection intercepted the request."
                )
                _dump_debug_html(target["name"], page, html, "redirected")
                break

            page_listings = parse_listings(html)
            if not page_listings:
                log.warning(f"[{target['name']}] page {page} returned real search HTML but 0 listings parsed - "
                            f"selectors may be stale, dumping HTML for inspection.")
                _dump_debug_html(target["name"], page, html, "empty_parse")
                break

            new_on_page = [l for l in page_listings if l["item_id"] not in seen_ids]
            if not new_on_page:
                break  # repeat page, likely end of results

            for listing in new_on_page:
                seen_ids.add(listing["item_id"])
            all_listings.extend(new_on_page)

            referer = url
            if page < max_pages:
                await asyncio.sleep(random.uniform(delay_min, delay_max))

    return all_listings
