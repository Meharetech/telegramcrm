import asyncio
import logging
import re
import random
from collections import OrderedDict
from datetime import datetime
import zoneinfo

logger = logging.getLogger(__name__)


async def resolve_variables(text: str, event, client, settings=None) -> str:
    """
    Resolve {{variable}} placeholders in a message template.

    Supported variables:
      👤 User:   {{first_name}}, {{last_name}}, {{username}}, {{user_id}}, {{phone}}, {{bio}}
      📢 Group:  {{group_name}}, {{group_id}}, {{member_count}}
      📅 System: {{date}}, {{time}}, {{today}}, {{random_number}}
    """
    if "{{" not in text:
        return text

    # ── Resolve sender info ──────────────────────────────────────────────────
    first_name = last_name = username = user_id = phone = bio = ""
    try:
        sender = await event.get_sender()
        if sender:
            first_name = getattr(sender, "first_name", "") or ""
            last_name  = getattr(sender, "last_name",  "") or ""
            username   = f"@{sender.username}" if getattr(sender, "username", None) else ""
            user_id    = str(getattr(sender, "id", ""))
            phone      = getattr(sender, "phone", "") or ""
            bio        = getattr(sender, "about", "") or ""
    except Exception:
        pass

    # ── Resolve group/channel info ───────────────────────────────────────────
    group_name = group_id = member_count = ""
    try:
        if event.is_group or event.is_channel:
            chat = await event.get_chat()
            group_name   = getattr(chat, "title", "") or ""
            group_id     = str(getattr(chat, "id", ""))
            member_count = str(getattr(chat, "participants_count", ""))
    except Exception:
        pass

    # ── System variables ─────────────────────────────────────────────────────
    tz_str = getattr(settings, "timezone", "Asia/Kolkata") if settings else "Asia/Kolkata"
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()

    mapping = {
        "first_name":    first_name or "Friend",
        "last_name":     last_name,
        "username":      username,
        "user_id":       user_id,
        "phone":         phone,
        "bio":           bio,
        "group_name":    group_name,
        "group_id":      group_id,
        "member_count":  member_count,
        "date":          now.strftime("%d %B %Y"),
        "time":          now.strftime("%I:%M %p"),
        "today":         now.strftime("%A, %d %B"),
        "random_number": str(random.randint(1000, 9999)),
    }

    def spintax_replacer(text):
        pattern = re.compile(r'\{([^{}]*)\}')
        while True:
            match = pattern.search(text)
            if not match: break
            choices = match.group(1).split('|')
            text = text.replace(match.group(0), random.choice(choices), 1)
        return text

    # Apply Spintax first, then variable substitution
    text = spintax_replacer(text)

    def replacer(match):
        key = match.group(1).strip()
        return mapping.get(key, match.group(0))  # keep original if unknown

    return re.sub(r"\{\{(\w+)\}\}", replacer, text)


def is_daytime(settings) -> bool:
    """
    Returns True if current local time is within the configured Day window.
    Night window is everything OUTSIDE the day window.
    Example: day_start=09:00 AM, day_end=11:00 PM → Night = 23:00–09:00 IST
    """
    try:
        tz_str = getattr(settings, "timezone", "Asia/Kolkata")
        tz = zoneinfo.ZoneInfo(tz_str)
        now_tz = datetime.now(tz)
        current_hhmm = now_tz.strftime("%H:%M")

        raw_start = getattr(settings, "day_start", "09:00")
        raw_end   = getattr(settings, "day_end",   "09:00")
        ampm_start = getattr(settings, "day_start_ampm", "AM")
        ampm_end   = getattr(settings, "day_end_ampm",   "PM")

        def to_24h(hhmm: str, ampm: str) -> str:
            parts = hhmm.split(":")
            h = int(parts[0]) % 12   # normalise 12 → 0 first
            m = parts[1] if len(parts) > 1 else "00"
            if ampm == "PM":
                h += 12              # 0 PM=12, 1 PM=13 … 11 PM=23
            return f"{h:02d}:{m}"

        start_24 = to_24h(raw_start, ampm_start)
        end_24   = to_24h(raw_end,   ampm_end)

        logger.info(
            f"[auto-reply] Time Check | Local: {current_hhmm} "
            f"| DayWindow: {start_24}–{end_24} | Zone: {tz_str}"
        )

        # If start < end  → simple range  (e.g. 09:00–21:00)
        # If start >= end → crosses midnight (e.g. 22:00–06:00)
        if start_24 < end_24:
            is_day = start_24 <= current_hhmm <= end_24
        else:
            # Day crosses midnight: daytime is start→midnight→end
            is_day = current_hhmm >= start_24 or current_hhmm <= end_24

        logger.info(f"[auto-reply] Is Daytime? {is_day}")
        return is_day

    except Exception as e:
        logger.warning(f"[auto-reply] Daytime check error: {e}")
        return False   # Fail-safe: assume Night so reply fires


# ── FIX: Bounded LRU dict prevents _welcome_locks from growing forever ────────
# Previously this was an unbounded plain dict: every unique sender_id was added
# and never removed — a definite memory leak at 1000+ users.
class _BoundedDict(OrderedDict):
    """OrderedDict with a max size. Evicts oldest entry when full (LRU-like)."""
    def __init__(self, maxsize: int = 5000):
        self._maxsize = maxsize
        super().__init__()

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self._maxsize:
            self.popitem(last=False)   # evict oldest
        super().__setitem__(key, value)


# Per-sender asyncio locks: prevents two concurrent messages from the same
# sender both passing the history check before the welcome is sent.
_welcome_locks: _BoundedDict = _BoundedDict(maxsize=5000)


async def should_trigger_welcome(client, sender_id: int, event=None) -> bool:
    """
    Returns True if a welcome message should be sent to this sender.

    Decision is based purely on Telegram's actual message history:
      ≤ 1 message in history → new / freshly-cleared conversation → True
      ≥ 2 messages           → existing contact                   → False

    A per-sender asyncio.Lock prevents rapid successive messages from the
    same sender both passing the history check and triggering double-fires.
    """
    # Guard: only works for real private DMs (sender_id must be a positive int)
    if not isinstance(sender_id, int) or sender_id <= 0:
        return False

    if sender_id not in _welcome_locks:
        _welcome_locks[sender_id] = asyncio.Lock()

    async with _welcome_locks[sender_id]:
        try:
            # Resolve peer entity from the event to avoid "input entity not found"
            peer = None
            if event is not None:
                try:
                    peer = await event.get_input_sender()
                except Exception:
                    pass

            # Fallback to raw sender_id if event resolution failed
            if peer is None:
                peer = sender_id

            # Use iter_messages (safe async generator) instead of get_messages
            # to avoid the slice(None, 10, None) Telethon bug with entity+limit
            msgs = []
            async for m in client.iter_messages(entity=peer, limit=2):
                msgs.append(m)
            msg_count = len(msgs)

            logger.info(f"[auto-reply] History check for {sender_id}: {msg_count} message(s)")

            if msg_count <= 1:
                logger.info(f"[auto-reply] Fresh conversation — sending welcome to {sender_id}")
                return True

            logger.info(f"[auto-reply] Existing contact ({msg_count} msgs) — skipping welcome for {sender_id}")
            return False

        except Exception as e:
            logger.warning(f"[auto-reply] History check error for {sender_id}: {e} — sending welcome")
            return True   # On error: assume new user so welcome isn't silently dropped


def matches_rule(msg_text: str, rule) -> bool:
    """Determine if the message text triggers the given rule."""
    if rule.trigger_type == "any":
        return True

    if rule.trigger_type == "keyword" and rule.keywords:
        haystack = msg_text if rule.case_sensitive else msg_text.lower()
        for kw in rule.keywords:
            needle = kw if rule.case_sensitive else kw.lower()
            if rule.match_mode == "exact":
                if haystack == needle: return True
            elif rule.match_mode == "startswith":
                if haystack.startswith(needle): return True
            else:  # contains
                if needle in haystack: return True
    return False
