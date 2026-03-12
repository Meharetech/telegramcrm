import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.models.plan import Plan
from app.config import settings

async def discount_yearly():
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    await init_beanie(database=client[settings.DATABASE_NAME], document_models=[Plan])
    
    plans = await Plan.find_all().to_list()
    for p in plans:
        # Yearly price = 10 * Monthly price (2 months free discount)
        p.price_yearly_inr = p.price_inr * 10
        await p.save()
        print(f"Discounted Yearly price for {p.name}: ₹{p.price_yearly_inr}")

if __name__ == "__main__":
    asyncio.run(discount_yearly())
