"""
Deterministic filtering, applied before anything reaches Ollama.

1. passes_hard_filters(): price range + include/exclude keywords.
   Target-level settings are combined with the global defaults in config.json.
2. is_price_outlier(): simple z-score check against the running mean/stdev
   of prices seen so far for that search target. Only outliers get sent
   to the (comparatively expensive) Ollama audit step.
"""

from __future__ import annotations

from typing import Optional


def passes_hard_filters(listing: dict, target: dict, config: dict) -> tuple[bool, str]:
    rules_cfg = config["rules"]

    price = listing.get("price")
    price_min = target.get("price_min", rules_cfg.get("default_price_min"))
    price_max = target.get("price_max", rules_cfg.get("default_price_max"))

    if price is None:
        return False, "no_price"
    if price_min is not None and price < price_min:
        return False, "below_price_min"
    if price_max is not None and price > price_max:
        return False, "above_price_max"

    title = (listing.get("title") or "").lower()

    exclude_kw = [*rules_cfg.get("global_exclude_keywords", []), *target.get("exclude_keywords", [])]
    for kw in exclude_kw:
        if kw.lower() in title:
            return False, f"excluded_keyword:{kw}"

    include_kw = [*rules_cfg.get("global_include_keywords", []), *target.get("include_keywords", [])]
    if include_kw and not any(kw.lower() in title for kw in include_kw):
        return False, "missing_include_keyword"

    return True, "ok"


def is_price_outlier(
    price: Optional[float],
    mean: Optional[float],
    stdev: Optional[float],
    sample_count: int,
    config: dict,
) -> bool:
    rules_cfg = config["rules"]
    min_samples = rules_cfg.get("outlier_min_samples", 5)
    threshold = rules_cfg.get("outlier_zscore_threshold", 1.5)

    if price is None or mean is None or stdev is None:
        return False
    if sample_count < min_samples or stdev == 0:
        return False

    z = (price - mean) / stdev
    return abs(z) >= threshold
