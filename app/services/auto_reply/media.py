import os
import logging

logger = logging.getLogger(__name__)

async def send_rule_media(client, event, rule):
    """Handle sending different types of media (Telegram-hosted and local)."""
    local_media = getattr(rule, "media_paths", [])
    tg_media_list = getattr(rule, "tg_media", [])

    # 1. Telegram-hosted media (Saved Messages reference)
    if tg_media_list:
        for m_item in tg_media_list:
            if isinstance(m_item, dict) and 'media' in m_item:
                try:
                    m = m_item['media']
                    caption = m_item.get('caption', '')
                    if m.get('type') == 'saved_msg':
                        saved = await client.get_messages('me', ids=int(m['msg_id']))
                        if saved and saved.media:
                            await client.send_file(
                                entity=event.chat_id,
                                file=saved.media,
                                caption=caption,
                                reply_to=event.message.id
                            )
                except Exception as me:
                    logger.error(f"[auto-reply] Failed to send TG media: {me}")

    # 2. Local files (Fallback)
    valid_local = [p for p in local_media if os.path.exists(p)]
    if valid_local:
        for fpath in valid_local:
            try:
                await event.reply(file=fpath)
            except Exception as le:
                logger.error(f"[auto-reply] Failed to send local file {fpath}: {le}")

async def mark_read(client, chat_id):
    """Acknowledge receipt of the message."""
    try:
        await client.send_read_acknowledge(chat_id, max_id=0, clear_mentions=True)
    except Exception as e:
        logger.warning(f"[auto-reply] Mark read failed: {e}")
