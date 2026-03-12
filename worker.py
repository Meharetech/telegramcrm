import asyncio
from telethon import TelegramClient, events
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_client(session_name, api_id, api_hash):
    client = TelegramClient(session_name, api_id, api_hash)
    
    @client.on(events.NewMessage)
    async def handler(event):
        logger.info(f"New message from {event.chat_id}: {event.text}")
        
    await client.start()
    logger.info(f"Client {session_name} started")
    await client.run_until_disconnected()

if __name__ == "__main__":
    # This would be triggered by a Celery worker or separate process
    print("Telegram Worker Service Placeholder")
