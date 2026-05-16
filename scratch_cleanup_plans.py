import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
import sys
import os

# Add backend to path
sys.path.append(os.getcwd())

from app.models.plan import Plan
from app.config import settings

async def cleanup():
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    await init_beanie(database=client[settings.DATABASE_NAME], document_models=[Plan])
    
    # Delete old 7-day trial if it exists
    old_trial = await Plan.find_one(Plan.name == "7-Day Free Trial")
    if old_trial:
        await old_trial.delete()
        print("Deleted old '7-Day Free Trial' plan.")
    else:
        print("Old trial plan not found.")
        
    plans = await Plan.find_all().to_list()
    print("Current Plans:", [p.name for p in plans])

if __name__ == "__main__":
    asyncio.run(cleanup())
