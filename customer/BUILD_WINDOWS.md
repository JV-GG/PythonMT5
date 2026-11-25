# Building the Windows download (BeyondSignalBot.exe)

MetaTrader5's Python library is Windows-only, so the `.exe` must be built on a Windows PC or VPS.
Do this once per release; customers only download the result.

## On a Windows machine

1. Install Python 3.12 (64-bit) and this bot's folder.
2. In a terminal, from the bot folder:

   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt pyinstaller
   ```

3. Build a single-file app (bundles the config but NOT your .env):

   ```
   pyinstaller --onefile --name BeyondSignalBot ^
     --collect-all MetaTrader5 --collect-all keyring ^
     main.py
   ```

   The app appears at `dist\BeyondSignalBot.exe`.

4. Make the customer zip: put together
   - `dist\BeyondSignalBot.exe`
   - `customer\settings.example.txt`
   - a short `README.txt` (rename settings.example.txt to .env, fill it in, run the exe, enter the code at signal.beyond-signal.net/bot).

5. Upload the zip somewhere the portal can link to, and set on the server:
   - `BOT_DOWNLOAD_URL` = the zip's URL
   - `BOT_VERSION` = e.g. 1.0.0  (must be ≥ the server's `minClientVersion`)

## Notes
- The `.exe` carries no API key. On first run it links to the user's Premium account and stores a
  per-device key in the Windows Credential Manager.
- To force everyone to update, raise `BOT_MIN_CLIENT_VERSION` in `lib/botRules.ts` and ship a build
  whose `portal_link.CLIENT_VERSION` is at least that. Older bots then refuse to start.
