"""
SQLite storage layer (async via aiosqlite).

Table `listings` holds one row per eBay item (deduped by item_id). Each
poll cycle either inserts a new row or refreshes `last_seen` / `price`
on an existing one.
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT UNIQUE NOT NULL,
    search_target TEXT NOT NULL,
    title TEXT,
    price REAL,
    currency TEXT,
    condition TEXT,
    url TEXT,
    image_url TEXT,
    location TEXT,
    first_seen TEXT,
    last_seen TEXT,
    is_outlier INTEGER DEFAULT 0,
    ollama_verdict TEXT,
    ollama_confidence REAL,
    ollama_reasoning TEXT,
    audited_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_target ON listings(search_target);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_listing(db_path: str, listing: dict, target_name: str) -> bool:
    """
    Insert a new listing or refresh last_seen/price on an existing one.
    Returns True if this was a brand-new listing (first time seen).
    """
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT id, price FROM listings WHERE item_id = ?", (listing["item_id"],)
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                """
                INSERT INTO listings
                    (item_id, search_target, title, price, currency, condition,
                     url, image_url, location, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing["item_id"],
                    target_name,
                    listing.get("title"),
                    listing.get("price"),
                    listing.get("currency", "USD"),
                    listing.get("condition"),
                    listing.get("url"),
                    listing.get("image_url"),
                    listing.get("location"),
                    now,
                    now,
                ),
            )
            await db.commit()
            return True
        else:
            await db.execute(
                "UPDATE listings SET price = ?, last_seen = ? WHERE item_id = ?",
                (listing.get("price"), now, listing["item_id"]),
            )
            await db.commit()
            return False


async def get_price_stats(
    db_path: str, target_name: str, exclude_item_id: Optional[str] = None
) -> tuple[Optional[float], Optional[float], int]:
    """
    Returns (mean, stdev, sample_count) of known prices for a search target.

    exclude_item_id should always be passed as the listing currently being
    judged - otherwise its own price is part of the baseline it's being
    compared against, which dilutes the mean/stdev and makes genuine
    outliers less likely to trip the threshold (worse for the exact
    listings this is meant to catch, and worse the smaller the sample).
    """
    query = "SELECT price FROM listings WHERE search_target = ? AND price IS NOT NULL"
    params: list = [target_name]
    if exclude_item_id:
        query += " AND item_id != ?"
        params.append(exclude_item_id)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    prices = [r[0] for r in rows if r[0] is not None]
    if len(prices) < 2:
        return (prices[0] if prices else None, None, len(prices))
    return (statistics.mean(prices), statistics.stdev(prices), len(prices))


async def get_listing(db_path: str, item_id: str) -> Optional[dict]:
    """Fetch a single listing's current row (pre-update state), or None if unseen."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM listings WHERE item_id = ?", (item_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def mark_outlier(db_path: str, item_id: str, is_outlier: bool) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE listings SET is_outlier = ? WHERE item_id = ?",
            (1 if is_outlier else 0, item_id),
        )
        await db.commit()


async def save_audit(db_path: str, item_id: str, verdict: str, confidence: Optional[float], reasoning: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE listings
            SET ollama_verdict = ?, ollama_confidence = ?, ollama_reasoning = ?, audited_at = ?
            WHERE item_id = ?
            """,
            (verdict, confidence, reasoning, _now(), item_id),
        )
        await db.commit()


async def fetch_listings(
    db_path: str,
    target: Optional[str] = None,
    verdict: Optional[str] = None,
    outliers_only: bool = False,
    limit: int = 300,
) -> list[dict]:
    query = "SELECT * FROM listings WHERE 1=1"
    params: list = []
    if target:
        query += " AND search_target = ?"
        params.append(target)
    if verdict:
        query += " AND ollama_verdict = ?"
        params.append(verdict)
    if outliers_only:
        query += " AND is_outlier = 1"
    query += " ORDER BY last_seen DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def distinct_targets(db_path: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT DISTINCT search_target FROM listings ORDER BY search_target")
        rows = await cur.fetchall()
    return [r[0] for r in rows]
