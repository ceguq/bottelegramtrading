import ast
import re
from telethon import TelegramClient

text = open("telegram_listener.py", "r", encoding="utf-8").read()

api_id = int(re.search(r"API_ID\s*=\s*(\d+)", text).group(1))
api_hash = ast.literal_eval(re.search(r"API_HASH\s*=\s*(.+)", text).group(1).split("#")[0].strip())
phone = ast.literal_eval(re.search(r"PHONE\s*=\s*(.+)", text).group(1).split("#")[0].strip())

client = TelegramClient("session_find_id", api_id, api_hash)

async def main():
    print("Connecting Telegram...")
    await client.start(phone=phone)
    print("Logged in. Listing chats...")
    async for dialog in client.iter_dialogs():
        print("Nama:", dialog.name)
        print("ID:", dialog.id)
        print("-" * 40)

with client:
    client.loop.run_until_complete(main())
