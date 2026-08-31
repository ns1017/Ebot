"""
Entry point. Offers a menu (or --mode flag for non-interactive use) so
you can run just the piece you need:

  1) Full pipeline - scrape loop + dashboard together
  2) Dashboard only - serve existing data, no scraping
  3) Ollama audit pass - one-shot: audit any flagged outliers that
     don't have a verdict yet, then exit (useful for catching up after
     enabling Ollama, or after a target ran with it turned off)
  4) Exit

Run with: python main.py            (shows the menu)
       or: python main.py --mode full
       or: python main.py --mode dashboard
       or: python main.py --mode ollama
Stop long-running modes with: Ctrl+C
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from modules import db, ebot, ollama_audit, rules
from modules.dashboard import app as dashboard_app

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
TARGETS_PATH = ROOT / "search_targets.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("main")

MENU_TEXT = """
Automotive Listings Dashboard
------------------------------
1) Start polling + host dashboard (full pipeline)
2) Host dashboard only (view existing data, no scraping)
3) Run Ollama audit pass (audit any flagged-but-unaudited outliers, then exit)
4) Exit
"""


def load_json(path: Path) -> dict | list:
    with open(path, "r") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Core listing pipeline
# --------------------------------------------------------------------------

async def process_listing(listing: dict, target: dict, config: dict, db_path: str) -> None:
    ok, reason = rules.passes_hard_filters(listing, target, config)
    if not ok:
        return

    # Look up any existing row and compute price stats BEFORE writing this
    # listing's price to the DB, and explicitly excluding this item_id from
    # the stats query. Both matter: if we upserted first (or didn't
    # exclude), this listing's own price becomes part of the mean/stdev
    # it's being judged against, which dilutes the signal and makes real
    # outliers less likely to trip the threshold.
    existing = await db.get_listing(db_path, listing["item_id"])
    mean, stdev, count = await db.get_price_stats(db_path, target["name"], exclude_item_id=listing["item_id"])
    outlier = rules.is_price_outlier(listing.get("price"), mean, stdev, count, config)

    is_new = await db.upsert_listing(db_path, listing, target["name"])
    await db.mark_outlier(db_path, listing["item_id"], outlier)

    tag = "NEW" if is_new else "seen"
    log.info(f"[{target['name']}] {tag} ${listing.get('price')} - {listing['title'][:60]}"
              f"{' (OUTLIER)' if outlier else ''}")

    if not (outlier and config["ollama"].get("enabled", True)):
        return

    # Only (re)audit if this is genuinely new information: no verdict yet,
    # or the price has changed since the last audit. Otherwise an unsold
    # outlier would get re-audited every single poll cycle indefinitely
    # for no new information.
    needs_audit = (
        existing is None
        or not existing.get("ollama_verdict")
        or existing.get("price") != listing.get("price")
    )
    if not needs_audit:
        return

    verdict = await ollama_audit.audit_listing(listing, mean, count, config)
    await db.save_audit(
        db_path, listing["item_id"], verdict["verdict"], verdict["confidence"], verdict["reasoning"]
    )
    log.info(f"  -> ollama verdict: {verdict['verdict']} ({verdict['confidence']:.2f}) {verdict['reasoning']}")


async def run_cycle(targets: list[dict], config: dict, db_path: str) -> None:
    scraper_cfg = config["scraper"]
    for target in targets:
        if not target.get("enabled", False):
            continue
        try:
            listings = await ebot.search_target(target, config)
        except Exception:
            log.exception(f"Scrape failed for target '{target['name']}'")
            continue

        log.info(f"[{target['name']}] fetched {len(listings)} listings")
        for listing in listings:
            await process_listing(listing, target, config, db_path)

        await asyncio.sleep(random.uniform(
            scraper_cfg["delay_between_requests_min_seconds"],
            scraper_cfg["delay_between_requests_max_seconds"],
        ))


async def scrape_loop(config: dict, db_path: str, stop_event: asyncio.Event) -> None:
    scraper_cfg = config["scraper"]
    while not stop_event.is_set():
        targets = load_json(TARGETS_PATH)
        enabled = [t for t in targets if t.get("enabled", False)]

        if not enabled:
            log.warning("No enabled search targets in search_targets.json - nothing to scrape this cycle.")
        else:
            await run_cycle(targets, config, db_path)

        wait_s = random.uniform(
            scraper_cfg["poll_interval_min_seconds"],
            scraper_cfg["poll_interval_max_seconds"],
        )
        log.info(f"Cycle done. Sleeping ~{int(wait_s / 60)} min before next poll.")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# Standalone Ollama audit pass (mode 3)
# --------------------------------------------------------------------------

async def run_ollama_audit_pass(config: dict, db_path: str) -> None:
    await ollama_audit.check_ollama_available(config)

    outliers = await db.fetch_listings(db_path, outliers_only=True, limit=1000)
    pending = [l for l in outliers if not l.get("ollama_verdict")]

    if not pending:
        log.info("No un-audited outliers found in the database - nothing to do.")
        return

    log.info(f"Running Ollama audit on {len(pending)} un-audited outlier(s)...")
    for listing in pending:
        mean, stdev, count = await db.get_price_stats(
            db_path, listing["search_target"], exclude_item_id=listing["item_id"]
        )
        verdict = await ollama_audit.audit_listing(listing, mean, count, config)
        await db.save_audit(
            db_path, listing["item_id"], verdict["verdict"], verdict["confidence"], verdict["reasoning"]
        )
        log.info(
            f"[{listing['search_target']}] {listing['item_id']} (${listing.get('price')}) -> "
            f"{verdict['verdict']} ({verdict['confidence']:.2f}) {verdict['reasoning']}"
        )
    log.info("Ollama audit pass complete.")


# --------------------------------------------------------------------------
# Dashboard server (shared by modes 1 and 2)
# --------------------------------------------------------------------------

async def serve_dashboard(config: dict, db_path: str, stop_event: asyncio.Event) -> None:
    dashboard_app.state.db_path = db_path
    dashboard_app.state.config = config

    uv_config = uvicorn.Config(
        dashboard_app,
        host=config["dashboard"]["host"],
        port=config["dashboard"]["port"],
        log_level="warning",
    )
    server = uvicorn.Server(uv_config)
    log.info(f"Dashboard: http://{config['dashboard']['host']}:{config['dashboard']['port']}/")

    server_task = asyncio.create_task(server.serve())
    await stop_event.wait()
    log.info("Shutting down dashboard...")
    server.should_exit = True
    await server_task


def _install_signal_handlers(stop_event: asyncio.Event) -> list[signal.Signals]:
    """Returns the list of signals actually hooked, so they can be cleanly
    un-hooked afterward - otherwise a stale handler tied to a stop_event
    that's no longer being awaited by anything would silently swallow a
    later Ctrl+C at the menu prompt instead of exiting the program."""
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed.append(sig)
        except NotImplementedError:
            pass  # signal handlers aren't available on some platforms
    return installed


def _remove_signal_handlers(sigs: list[signal.Signals]) -> None:
    loop = asyncio.get_running_loop()
    for sig in sigs:
        try:
            loop.remove_signal_handler(sig)
        except NotImplementedError:
            pass


# --------------------------------------------------------------------------
# Mode entry points
# --------------------------------------------------------------------------

async def run_full(config: dict, db_path: str) -> None:
    stop_event = asyncio.Event()
    installed = _install_signal_handlers(stop_event)
    print("Running full pipeline. Press Ctrl+C to stop and return to the menu.\n")
    try:
        scraper_task = asyncio.create_task(scrape_loop(config, db_path, stop_event))
        dashboard_task = asyncio.create_task(serve_dashboard(config, db_path, stop_event))

        await stop_event.wait()
        log.info("Stopping...")
        scraper_task.cancel()
        await asyncio.gather(dashboard_task, scraper_task, return_exceptions=True)
    finally:
        _remove_signal_handlers(installed)


async def run_dashboard_only(config: dict, db_path: str) -> None:
    stop_event = asyncio.Event()
    installed = _install_signal_handlers(stop_event)
    log.info("Dashboard-only mode - not scraping. Showing existing data from the database.")
    print("Press Ctrl+C to stop and return to the menu.\n")
    try:
        await serve_dashboard(config, db_path, stop_event)
    finally:
        _remove_signal_handlers(installed)


async def run_ollama_only(config: dict, db_path: str) -> None:
    await run_ollama_audit_pass(config, db_path)


async def run_mode(mode: str, config: dict, db_path: str) -> None:
    if mode == "full":
        await ollama_audit.check_ollama_available(config)
        await run_full(config, db_path)
    elif mode == "dashboard":
        await run_dashboard_only(config, db_path)
    elif mode == "ollama":
        await run_ollama_only(config, db_path)


# --------------------------------------------------------------------------
# Menu / CLI entry
# --------------------------------------------------------------------------

def prompt_menu() -> str:
    print(MENU_TEXT)
    while True:
        try:
            choice = input("Choose an option [1-4]: ").strip()
        except EOFError:
            # No interactive stdin available (e.g. piped/non-tty) - exit
            # cleanly instead of looping forever on an exhausted input.
            print("\nNo input available - exiting.")
            sys.exit(0)
        if choice == "1":
            return "full"
        if choice == "2":
            return "dashboard"
        if choice == "3":
            return "ollama"
        if choice == "4":
            print("Bye.")
            sys.exit(0)
        print("Not a valid option, try again.")


def parse_args() -> Optional[str]:
    parser = argparse.ArgumentParser(description="Automotive listings dashboard")
    parser.add_argument(
        "--mode",
        choices=["full", "dashboard", "ollama"],
        default=None,
        help="Skip the interactive menu and run this mode directly, then exit "
             "(instead of looping back to the menu - use this for scripts/services).",
    )
    return parser.parse_args().mode


async def main() -> None:
    config = load_json(CONFIG_PATH)
    db_path = str(ROOT / config["database"]["path"])
    await db.init_db(db_path)

    cli_mode = parse_args()
    if cli_mode:
        # Explicit --mode: run once and exit, no interactive menu involved.
        await run_mode(cli_mode, config, db_path)
        return

    # Interactive: keep returning to the menu after a mode stops (Ctrl+C on
    # a long-running mode, or the one-shot Ollama pass finishing) instead of
    # exiting the whole program. Only "4) Exit" (or Ctrl+C at the menu
    # itself) ends it.
    while True:
        mode = prompt_menu()
        await run_mode(mode, config, db_path)
        print("\nBack to menu.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
