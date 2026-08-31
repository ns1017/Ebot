"""
Sends outlier listings to a local Ollama model for a lightweight second
opinion. Only called for listings that already tripped the statistical
outlier check in rules.py - keeps LLM calls rare and cheap.

Requires a running Ollama daemon (`ollama serve`) with the configured
model pulled, e.g. `ollama pull llama3.2:3b`.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("ollama_audit")

PROMPT_TEMPLATE = """You are auditing a used-parts/vehicle listing scraped from eBay for a buyer \
who wants to know if it's worth a closer look.

Listing:
- Title: {title}
- Price: ${price}
- Condition: {condition}
- Location: {location}

Market context for this search:
- Average price seen for similar search results: ${mean}
- Sample size: {count} listings

Judge whether this looks like a genuine good deal, a normal listing, or something with \
scam-risk red flags (price far too low for the item type, vague/generic title, common \
scam wording, mismatched condition vs price, etc). You only have the title/price/condition, \
not the full description or photos, so keep confidence calibrated to that.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"verdict": "good_deal" | "scam_risk" | "normal" | "unclear", "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}}
"""


def _fallback(reasoning: str) -> dict:
    return {"verdict": "unclear", "confidence": 0.0, "reasoning": reasoning}


async def check_ollama_available(config: dict) -> None:
    """
    Soft startup check - logs whether Ollama is reachable and whether
    the configured model is actually pulled, so problems show up
    immediately in the logs instead of silently as a 60-120s timeout
    the first time an outlier gets audited.
    """
    ollama_cfg = config["ollama"]
    if not ollama_cfg.get("enabled", True):
        log.info("Ollama audit is disabled in config.json.")
        return

    host = ollama_cfg["host"].rstrip("/")
    model = ollama_cfg["model"]
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{host}/api/tags", timeout=5)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        log.warning(
            f"Could not connect to Ollama at {host} - is `ollama serve` running? "
            f"Outlier audits will fail (and time out slowly) until it's reachable."
        )
        return
    except httpx.HTTPError as e:
        log.warning(f"Ollama health check at {host}/api/tags failed ({type(e).__name__}): {e}")
        return

    names = [m.get("name", "") for m in data.get("models", [])]
    # Ollama model names in /api/tags include a tag, e.g. "llama3.2:3b" -
    # match on either the exact string or the name before the colon.
    base_model = model.split(":")[0]
    if not any(n == model or n.startswith(base_model + ":") for n in names):
        log.warning(
            f"Ollama is running but model '{model}' doesn't appear to be pulled "
            f"(available: {names or 'none'}). Run: ollama pull {model}"
        )
    else:
        log.info(f"Ollama reachable at {host}, model '{model}' is available.")


async def audit_listing(listing: dict, mean: Optional[float], count: int, config: dict) -> dict:
    ollama_cfg = config["ollama"]
    if not ollama_cfg.get("enabled", True):
        return _fallback("ollama_disabled")

    prompt = PROMPT_TEMPLATE.format(
        title=listing.get("title", "unknown"),
        price=listing.get("price", "unknown"),
        condition=listing.get("condition") or "not listed",
        location=listing.get("location") or "not listed",
        mean=round(mean, 2) if mean is not None else "unknown",
        count=count,
    )

    url = f"{ollama_cfg['host'].rstrip('/')}/api/generate"
    payload = {
        "model": ollama_cfg["model"],
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    timeout = ollama_cfg.get("timeout_seconds", 120)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        log.warning(f"Could not connect to Ollama at {ollama_cfg['host']} - is `ollama serve` running?")
        return _fallback("ollama_connection_refused")
    except httpx.TimeoutException:
        log.warning(
            f"Ollama request timed out after {timeout}s. If this is the first audit since starting "
            f"Ollama, the model may still be loading into memory - consider raising "
            f"ollama.timeout_seconds in config.json, or check `ollama ps` to see if it's running."
        )
        return _fallback(f"ollama_timeout_after_{timeout}s")
    except httpx.HTTPStatusError as e:
        log.warning(f"Ollama returned an error status: {e.response.status_code} - {e.response.text[:200]}")
        return _fallback(f"ollama_http_{e.response.status_code}")
    except httpx.HTTPError as e:
        log.warning(f"Ollama request failed ({type(e).__name__}): {e}")
        return _fallback(f"ollama_request_failed:{type(e).__name__}")

    raw_text = data.get("response", "")
    try:
        parsed = json.loads(raw_text)
        verdict = parsed.get("verdict", "unclear")
        confidence = float(parsed.get("confidence", 0.0))
        reasoning = parsed.get("reasoning", "")
        if verdict not in ("good_deal", "scam_risk", "normal", "unclear"):
            verdict = "unclear"
        return {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
    except (json.JSONDecodeError, TypeError, ValueError):
        log.warning(f"Could not parse Ollama's response as JSON: {raw_text[:200]!r}")
        return _fallback("could_not_parse_model_output")
