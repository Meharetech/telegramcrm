import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from app.models import User, TelegramAccount, TelegramAPI, ReactionTask, Reminder, Proxy, SystemLog, MemberAddSettings, MemberAddJob, MessageCampaignJob
from app.models.auto_reply import AutoReplyRule, AutoReplySettings
from app.api.auth_utils import get_password_hash
from app.config import settings

async def create_admin():
    # ── Database Initialization ──
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    
    # We only need the User model to create an admin account
    await init_beanie(
        database=client[settings.DATABASE_NAME],
        document_models=[User]
    )

    admin_email = "admin@venom.id"
    admin_password = "admin123"
    
    existing_admin = await User.find_one(User.email == admin_email)
    
    if existing_admin:
        print(f"[*] Admin user {admin_email} already exists. Updating permissions...")
        existing_admin.is_admin = True
        existing_admin.is_admin_active = True
        existing_admin.is_active = True
        existing_admin.is_super_admin = True # Make super admin
        await existing_admin.save()
        print("[+] Admin and Super Admin permissions granted to existing user.")
    else:
        print(f"[*] Creating new admin user: {admin_email}...")
        hashed_password = get_password_hash(admin_password)
        new_admin = User(
            email=admin_email,
            hashed_password=hashed_password,
            full_name="System Administrator",
            is_admin=True,
            is_admin_active=True,
            is_active=True,
            is_super_admin=True # Make super admin
        )
        await new_admin.insert()
        print(f"[+] Admin user created successfully!")
        print(f"    Email: {admin_email}")
        print(f"    Password: {admin_password}")

if __name__ == "__main__":
    asyncio.run(create_admin())
