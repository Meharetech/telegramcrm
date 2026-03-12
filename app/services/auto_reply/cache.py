"""
cache.py — Auto-reply in-memory cache with TTL.

FIX: Original code had ZERO TTL — settings were fetched once on first message
and then NEVER refreshed unless invalidate_settings_cache() was explicitly called.
If any new endpoint forgot to call invalidate, settings would be permanently stale.

New behaviour:
  - Cache entry expires after SETTINGS_TTL seconds (60s) as a safety net
  - Explicit invalidation still works instantly (via invalidate_* functions)
  - Rules cache also gets a TTL (30s) — rules change more frequently
"""

import time
from typing import List, Optional, Dict, Tuple, Any
from app.models.auto_reply import AutoReplySettings, AutoReplyRule

# TTL constants
SETTINGS_TTL = 60   # seconds before auto-refresh
RULES_TTL    = 30   # seconds before auto-refresh (rules change more often)

# { account_id: (value, timestamp) }
_settings_cache: Dict[str, Tuple[Optional[AutoReplySettings], float]] = {}
_rules_cache:    Dict[str, Tuple[Optional[List[AutoReplyRule]], float]] = {}


async def get_cached_settings(account_id: str) -> Optional[AutoReplySettings]:
    """
    Return auto-reply settings from cache, refreshing if:
      - Not in cache (first access)
      - Cache entry is older than SETTINGS_TTL (safety TTL net)
      - Cache was explicitly invalidated via invalidate_settings_cache()
    """
    entry = _settings_cache.get(account_id)
    if entry is not None:
        value, ts = entry
        if time.monotonic() - ts < SETTINGS_TTL:
            return value  # Cache hit within TTL

    # Cache miss or expired — fetch fresh from DB
    value = await AutoReplySettings.find_one(
        AutoReplySettings.account_id == account_id
    )
    _settings_cache[account_id] = (value, time.monotonic())
    return value


async def get_cached_rules(account_id: str) -> List[AutoReplyRule]:
    """
    Return enabled auto-reply rules from cache, refreshing if expired
    or explicitly invalidated. Rules have a shorter TTL than settings.
    """
    entry = _rules_cache.get(account_id)
    if entry is not None:
        value, ts = entry
        if time.monotonic() - ts < RULES_TTL:
            return value or []  # Cache hit within TTL

    # Cache miss or expired — fetch fresh from DB
    rules = await AutoReplyRule.find(
        AutoReplyRule.account_id == account_id,
        AutoReplyRule.is_enabled == True
    ).to_list()
    _rules_cache[account_id] = (rules, time.monotonic())
    return rules or []


def invalidate_settings_cache(account_id: str) -> None:
    """Force immediate cache invalidation for settings of one account."""
    _settings_cache.pop(account_id, None)


def invalidate_rules_cache(account_id: str) -> None:
    """Force immediate cache invalidation for rules of one account."""
    _rules_cache.pop(account_id, None)


def invalidate_all_cache(account_id: str) -> None:
    """Invalidate both settings and rules caches for one account."""
    invalidate_settings_cache(account_id)
    invalidate_rules_cache(account_id)
