
import os
import time
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")

API_URL = f"https://api.telegram.org/bot{TOKEN}"
session = requests.Session()

def telegram_api(method, **params):
    response = session.post(
        f"{API_URL}/{method}",
        json=params,
        timeout=40,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data["result"]

def read_messages():
    # Start with pending updates, then listen continuously.
    offset = None
    print("Listening for Telegram messages. Press Ctrl+C to stop.")

    while True:
        try:
            params = {"timeout": 25, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset

            updates = telegram_api("getUpdates", **params)

            for update in updates:
                offset = update["update_id"] + 1

                msg = update.get("message", {})
                if not msg:
                    continue

                sender = msg.get("from", {})
                chat = msg.get("chat", {})
                text = msg.get("text", msg.get("caption", "[non-text message]"))

                timestamp = datetime.fromtimestamp(
                    msg.get("date", time.time())
                ).strftime("%Y-%m-%d %H:%M:%S")

                print("\n--- New message ---")
                print("Time:", timestamp)
                print("Sender:", sender.get("first_name", "Unknown"))
                print("User ID:", sender.get("id"))
                print("Chat ID:", chat.get("id"))
                print("Chat type:", chat.get("type"))
                print("Message:", text)

        except requests.RequestException as exc:
            print("Connection issue:", exc)
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nStopped.")
            break

if __name__ == "__main__":
    read_messages()
