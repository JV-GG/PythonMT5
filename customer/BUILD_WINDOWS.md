# Building the Windows download (BeyondSignalBot.exe)

The bot is now a **desktop app** (`gui.py`) — the customer types their MT5 login, ticks pairs and clicks
Start. No `.env`, no console. MetaTrader5's Python library is Windows-only, so the `.exe` must be built
on a Windows PC or VPS. Do this once per release; customers only download the result.

## On a Windows machine

1. Install Python 3.12 (64-bit) from python.org (it includes Tkinter, which the app needs) and copy
   this bot's folder over.
2. In a terminal, from the bot folder:

   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt pyinstaller
   ```

3. Build a single windowed app (no console window):

   ```
   pyinstaller --onefile --windowed --name BeyondSignalBot ^
     --collect-all MetaTrader5 --collect-all keyring --collect-all customtkinter ^
     --add-data "assets\pairs;assets\pairs" ^
     gui.py
   ```

   The app appears at `dist\BeyondSignalBot.exe`. Double-clicking it opens the window.

4. Make the customer zip (`BeyondSignalBot-1.0.0.zip`): put together
   - `dist\BeyondSignalBot.exe`
   - a short `README.txt` (see below)

5. Upload the zip somewhere the portal can link to, and set on the server:
   - `BOT_DOWNLOAD_URL` = the zip's URL
   - `BOT_VERSION` = e.g. 1.0.0  (must be ≥ the server's `minClientVersion`)

## README.txt for the zip

```
Beyond Signal Bot (Windows)

1. Make sure MetaTrader 5 is installed and you can log in to your account in it.
2. Unzip and run BeyondSignalBot.exe. Windows may ask you to allow it.
3. Click "Connect account", then enter the code it shows at
   https://signal.beyond-signal.net/bot  (sign in, Premium plan).
4. Make sure MetaTrader 5 is open and logged in to your broker, then pick your lot size and pairs.
5. Click "Start trading". Leave the window open (or minimised) while it trades.
   Closing the window stops the bot.

Your MT5 login is stored only on this PC, in the Windows Credential Manager.
```

## Notes
- The `.exe` carries no API key. On first run it links to the user's Premium account and stores a
  per-device key, and the MT5 login the customer types, in the Windows Credential Manager.
- Closing the window stops the bot — there is no hidden background trading.
- To force everyone to update, raise `BOT_MIN_CLIENT_VERSION` in the server's `lib/botRules.ts` and ship
  a build whose `portal_link.CLIENT_VERSION` is at least that. Older bots then refuse to start.
- A server-side bot (your own account, not a customer) still runs through `main.py` (FastAPI), unchanged.
