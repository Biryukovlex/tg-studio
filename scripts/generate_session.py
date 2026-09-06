"""One-time interactive login for the collector.

QR login is the default because Telegram does not reliably offer SMS login to
third-party clients. It can be approved from an already-authorized Telegram
mobile app. Phone/code login remains available as a fallback.

After login, the script prints a SESSION_STRING to paste into .env. The same
string works on your Hetzner server - no re-login needed.

Usage:
    python scripts/generate_session.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import webbrowser
from pathlib import Path

import qrcode
from qrcode.image.svg import SvgPathImage
from telethon import TelegramClient
from telethon.sessions import StringSession

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))


def ask_env_value(key: str, prompt: str) -> str:
    """Read key from .env if present, else prompt."""
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                if val:
                    print(f"Using {key} from .env")
                    return val
    return input(prompt).strip()


def write_qr_page(url: str, directory: Path, generation: int) -> Path:
    """Write a large browser-rendered QR page without sending the token online."""
    code = qrcode.QRCode(border=4)
    code.add_data(url)
    code.make(fit=True)
    svg_name = f"telegram-login-{generation}.svg"
    svg_path = directory / svg_name
    code.make_image(image_factory=SvgPathImage).save(svg_path)

    page_path = directory / "telegram-login.html"
    page_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="1">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram login</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #f4f6f8; color: #17212b;
      font: 18px -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    main {{ text-align: center; padding: 24px; }}
    img {{
      display: block; width: min(78vw, 640px); height: min(78vw, 640px);
      margin: 20px auto; background: white; image-rendering: pixelated;
    }}
    p {{ margin: 8px 0; }}
  </style>
</head>
<body>
  <main>
    <h1>Scan with Telegram</h1>
    <p>Settings &rarr; Devices &rarr; Link Desktop Device</p>
    <img src="{svg_name}" alt="Telegram login QR code">
    <p>This page and QR are local to your Mac.</p>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    page_path.chmod(0o600)
    svg_path.chmod(0o600)
    return page_path


async def login_with_qr(client: TelegramClient):
    print("\nOn your phone, open Telegram -> Settings -> Devices")
    print("-> Link Desktop Device, then scan the QR opened in your browser.\n")

    with tempfile.TemporaryDirectory(prefix="telegram-login-") as temp_dir:
        directory = Path(temp_dir)
        browser_opened = False
        generation = 0

        while True:
            generation += 1
            qr_login = await client.qr_login()

            # Register the update handler before exposing the QR to avoid a
            # fast scan racing ahead of Telethon's wait coroutine.
            wait_task = asyncio.create_task(qr_login.wait())
            await asyncio.sleep(0)

            page_path = write_qr_page(qr_login.url, directory, generation)
            if not browser_opened:
                browser_opened = webbrowser.open(page_path.as_uri(), new=1)
                if not browser_opened:
                    print(f"Open this local file in a browser: {page_path}")

            print("Waiting for the browser QR scan... (it refreshes if it expires)")
            try:
                return await wait_task
            except asyncio.TimeoutError:
                print("\nQR expired; generating a fresh one...\n")


async def amain() -> None:
    api_id = int(ask_env_value("API_ID", "Enter API_ID: "))
    api_hash = ask_env_value("API_HASH", "Enter API_HASH: ")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        print("\nChoose login method:")
        print("  1. QR code (recommended; no SMS needed)")
        print("  2. Phone number and Telegram login code")
        method = input("Method [1]: ").strip() or "1"

        if method == "2":
            await client.start()
            me = await client.get_me()
        else:
            me = await login_with_qr(client)

        session_string = client.session.save()  # type: ignore[attr-defined]
        print("\n" + "=" * 62)
        print("Success! Logged in as:", me.first_name, f"@{me.username}" if me.username else "")
        print("\nYour SESSION_STRING (paste into .env):\n")
        print(session_string)
        print("=" * 62)
        print("\nTip: keep it secret - it grants full access to this account.")
    finally:
        await client.disconnect()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
