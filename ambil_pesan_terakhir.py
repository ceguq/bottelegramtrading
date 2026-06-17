from telethon import TelegramClient
from telegram_listener import API_ID, API_HASH, PHONE, SOURCE_CHAT_ID

client = TelegramClient("session_find_id", API_ID, API_HASH)

async def main():
    await client.start(phone=PHONE)

    print("Reading recent messages from chat:", SOURCE_CHAT_ID)
    print("-" * 60)

    async for msg in client.iter_messages(SOURCE_CHAT_ID, limit=10):
        print("MESSAGE ID:", msg.id)
        print("DATE:", msg.date)
        print("TEXT:")
        print(msg.raw_text)
        print("-" * 60)

with client:
    client.loop.run_until_complete(main())
