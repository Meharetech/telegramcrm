import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.models.plan import Plan
from app.config import settings

async def create_default_plans():
    # 1. Initialize Beanie FIRST
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    await init_beanie(database=client[settings.DATABASE_NAME], document_models=[Plan])
    
    # 2. DEFINITION DATA
    plans_data = [
        {
            "name": "7-Day Free Trial",
            "description": "Full access to all premium features for 7 days. Test everything before you buy.",
            "price_inr": 0.0,
            "max_accounts": 1,
            "max_api_keys": 5,
            "max_proxies": 5,
            "max_auto_replies": 5,
            "max_autoreply_accounts": 1,
            "max_reaction_channels": 5,
            "max_forwarder_channels": 1,
            "max_bots": 1,
            "max_folder_accounts": 1,
            "ai_chatbot_limit": 200,
            "max_ai_agents": 1,
            "access_connect": True,
            "access_chat_message": True,
            "access_member_adding": True,
            "access_message_sender": True,
            "access_group_scraping": True,
            "access_folder_campaign": True,
            "access_folder_scraper": True,
            "access_group_joiner": True,
            "access_ban_checker": True,
            "access_creative_tools": True,
            "access_contacts_manager": True,
            "access_reminders": True,
            "access_terminal": False,
            "access_bot_hub": True,
            "access_ai_agent": True
        },
        {
            "name": "Venom Starter",
            "description": "Essential automation for beginners and small-scale testing.",
            "price_inr": 499.0,
            "max_accounts": 10,
            "max_api_keys": 10,
            "max_proxies": 10,
            "max_auto_replies": 10,
            "max_autoreply_accounts": 2,
            "max_reaction_channels": 50,
            "max_forwarder_channels": 2,
            "max_folder_accounts": 1,
            "ai_chatbot_limit": 1000,
            "max_ai_agents": 1,
            "access_connect": True,
            "access_chat_message": True, # Bulk send
            "access_member_adding": False,
            "access_message_sender": False,
            "access_group_scraping": False,
            "access_folder_campaign": False,
            "access_folder_scraper": False,
            "access_group_joiner": False,
            "access_ban_checker": False,
            "access_creative_tools": False,
            "access_contacts_manager": False,
            "access_reminders": False,
            "access_terminal": False,
            "access_ai_agent": True
        },
        {
            "name": "Venom PRO (Most Popular)",
            "description": "The Ultimate Profit Center. Includes high-volume scraper and member adder.",
            "price_inr": 2499.0,
            "max_accounts": 100,
            "max_api_keys": 100,
            "max_proxies": 100,
            "max_auto_replies": 100,
            "max_autoreply_accounts": 20,
            "max_reaction_channels": 200,
            "max_forwarder_channels": 10,
            "max_folder_accounts": 5,
            "ai_chatbot_limit": -1,
            "max_ai_agents": 5,
            "access_connect": True,
            "access_chat_message": True,
            "access_member_adding": True,
            "access_message_sender": True,
            "access_group_scraping": True,
            "access_folder_campaign": True,
            "access_folder_scraper": True,
            "access_group_joiner": True,
            "access_ban_checker": True,
            "access_creative_tools": True,
            "access_contacts_manager": True,
            "access_reminders": True,
            "access_terminal": False,
            "access_ai_agent": True
        },
        {
            "name": "Venom ELITE (Unlimited)",
            "description": "Full platform control for agencies. Unlimited accounts and API access.",
            "price_inr": 7999.0,
            "max_accounts": -1,
            "max_api_keys": -1,
            "max_proxies": -1,
            "max_auto_replies": -1,
            "max_autoreply_accounts": -1,
            "max_reaction_channels": -1,
            "max_forwarder_channels": -1,
            "max_folder_accounts": -1,
            "ai_chatbot_limit": -1,
            "max_ai_agents": -1,
            "access_connect": True,
            "access_chat_message": True,
            "access_member_adding": True,
            "access_message_sender": True,
            "access_group_scraping": True,
            "access_folder_campaign": True,
            "access_folder_scraper": True,
            "access_group_joiner": True,
            "access_ban_checker": True,
            "access_creative_tools": True,
            "access_contacts_manager": True,
            "access_reminders": True,
            "access_terminal": True,
            "access_ai_agent": True
        }
    ]
    
    # ── Insert ─────────────────────────────────────────────────────────────
    for data in plans_data:
        # Check if they exist first or just insert
        exists = await Plan.find_one(Plan.name == data["name"])
        if not exists:
            p = Plan(**data)
            await p.insert()
            print(f"Plan Created Success: {data['name']}")
        else:
            # OPTIONAL: Force update if exists to sync new fields
            await exists.update({"$set": data})
            print(f"Plan Sync Updated: {data['name']}")

if __name__ == "__main__":
    asyncio.run(create_default_plans())
