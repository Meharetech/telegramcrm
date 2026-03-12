from datetime import datetime
from telethon import types

def format_status(status):
    if not status:
        return ""
    if isinstance(status, types.UserStatusOnline):
        return "online"
    if isinstance(status, types.UserStatusOffline):
        # status.was_online is a datetime
        # Check if tzinfo exists, if not use naive comparison
        now = datetime.now(status.was_online.tzinfo) if status.was_online.tzinfo else datetime.utcnow()
        diff = now - status.was_online
        
        if diff.days > 0:
            if diff.days == 1: return "last seen yesterday"
            return f"last seen {diff.days} days ago"
        hours = diff.seconds // 3600
        if hours > 0: return f"last seen {hours} hours ago"
        minutes = (diff.seconds % 3600) // 60
        if minutes > 0: return f"last seen {minutes} mins ago"
        return "last seen just now"
    if isinstance(status, types.UserStatusRecently):
        return "last seen recently"
    if isinstance(status, types.UserStatusLastWeek):
        return "last seen last week"
    if isinstance(status, types.UserStatusLastMonth):
        return "last seen last month"
    return ""
