"""
Run this script once to find your Telegram channel ID, then paste it into
telegram_listener.py as SOURCE_CHAT_ID.
"""

# Install dependency:
# pip install telethon

from telethon.sync import TelegramClient


# Get API_ID and API_HASH from https://my.telegram.org
API_ID = 37673990  # API ID from https://my.telegram.org
API_HASH = "a9a7c7a933318f577f7d16aeb05a63db"  # API hash from https://my.telegram.org
PHONE = "+6281229995423"  # Your Telegram phone number, including country code

SESSION_NAME = "session_find_id"


with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
    client.start(phone=PHONE)

    for dialog in client.iter_dialogs():
        print(f"{dialog.id:>20}  |  {dialog.name}")

    print("-" * 80)
    print("Cari nama channel sinyal kamu di atas, lalu copy angka ID-nya ke SOURCE_CHAT_ID")
