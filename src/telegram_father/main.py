from datetime import datetime
import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram_father import gemini_browser

load_dotenv()

api_id_raw = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")

if not api_id_raw or not api_hash:
    raise ValueError("Missing API_ID or API_HASH in .env")

log_file = Path("incoming_messages.jsonl")

client = TelegramClient("session_name", int(api_id_raw), api_hash)
generation_lock = asyncio.Lock()


@client.on(events.NewMessage(incoming=True))
async def handle_incoming_message(event):
    sender = await event.get_sender()
    chat = await event.get_chat()

    sender_name = (
        getattr(sender, "username", None)
        or " ".join(
            part for part in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if part
        ).strip()
        or str(event.sender_id)
    )
    chat_title = getattr(chat, "title", None) or "Direct Message"
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "chat": chat_title,
        "sender": sender_name,
        "sender_id": event.sender_id,
        "message_id": event.id,
        "text": event.raw_text,
        "has_media": bool(event.media),
    }

    # 1. Print live on your PC terminal
    print(f"[{record['timestamp']}] ({chat_title}) {sender_name}: {event.raw_text}")
    if event.media:
        print("   -> [Includes media/attachment]")

    # 2. Append to a local file on your PC for later processing
    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    prompt = (event.raw_text or "").strip()
    if not prompt:
        return

    async with generation_lock:
        try:
            await client.send_message(event.sender_id, "Generating your PowerPoint. I’ll send it here when it’s ready.")
            presentation_path = await gemini_browser.generate_presentation(prompt)
            if presentation_path is None:
                await client.send_message(event.sender_id, "I couldn’t generate a downloadable PowerPoint for that prompt.")
                return

            await client.send_file(event.sender_id, str(presentation_path))
        except Exception as exc:
            print(f"Failed to generate or send PowerPoint for {event.sender_id}: {exc}")
            await client.send_message(event.sender_id, "There was a problem generating or sending your PowerPoint.")


def main():
    client.start()
    print("PC Telegram listener running. Waiting for incoming messages...")
    print(f"Saving logs to: {log_file.resolve()}")
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
