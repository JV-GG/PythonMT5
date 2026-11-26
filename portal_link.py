"""
Beyond Signal portal link (Phase 1 of the in-portal bot).

A customer build has no API key. On first run it links to the user's Beyond Signal account:
it shows a short code, the user approves it in the portal (signed in, Premium), and the bot
receives a one-time *device key*. The device key is stored in the Windows Credential Manager
(DPAPI, per Windows user) — never in a plain file beside the .exe.

For trading, the device key is swapped for a short (15-minute) access token used as the
`x-bot-token` header on signal requests, so the long-lived key stays off the wire and a sniffed
token expires fast. The token response also carries the exact pairs this account may trade
(admin-enabled ∩ plan ∩ ticked); the bot trades only those.

When the Premium plan has lapsed the token endpoint returns plan_ended: the bot keeps managing
open trades but opens no new ones (handled by the caller).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger("portal")

_SERVICE = "BeyondSignalBot"
_ACCOUNT = "device_key"
_TOKEN_SKEW = 60  # refresh a token this many seconds before it expires
CLIENT_VERSION = "1.1.2"


# ── device key storage ────────────────────────────────────────────────────────
def _keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


def _fallback_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".beyondsignal")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "device_key")


def load_device_key() -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            v = kr.get_password(_SERVICE, _ACCOUNT)
            if v:
                return v
        except Exception as e:
            logger.warning("Credential store read failed (%s); using local file", e)
    p = _fallback_path()
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def save_device_key(key: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, _ACCOUNT, key)
            return
        except Exception as e:
            logger.warning("Credential store write failed (%s); using local file", e)
    p = _fallback_path()
    with open(p, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def clear_device_key() -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            pass
    try:
        os.remove(_fallback_path())
    except FileNotFoundError:
        pass


# ── link flow ─────────────────────────────────────────────────────────────────
def link_start(server_url: str, device_name: str) -> tuple[str, str]:
    """Begin a link; returns (code to show the user, poll_secret to poll with)."""
    poll_secret = secrets.token_urlsafe(24)
    with httpx.Client(base_url=server_url, timeout=15.0) as c:
        r = c.post("/api/bot/link/start", json={"pollSecret": poll_secret, "deviceName": device_name})
        r.raise_for_status()
        return r.json()["code"], poll_secret


def link_poll(server_url: str, code: str, poll_secret: str) -> str | None:
    """One poll. Returns the saved device key once the user approves, else None."""
    with httpx.Client(base_url=server_url, timeout=15.0) as c:
        pr = c.post("/api/bot/link/poll", json={"code": code, "pollSecret": poll_secret})
        if pr.status_code == 200 and pr.json().get("status") == "linked":
            key = pr.json()["key"]
            save_device_key(key)
            return key
    return None


def link_interactive(server_url: str, device_name: str, show=print, timeout_s: int = 600) -> str:
    """Console link: start, show the code, wait for approval, return the saved device key."""
    code, poll_secret = link_start(server_url, device_name)
    show("\n" + "=" * 44)
    show("  Link this bot to your Beyond Signal account")
    show(f"  1. Open:  {server_url}/bot/link")
    show("  2. Sign in (Premium) and enter this code:")
    show(f"\n        {code}\n")
    show("  Waiting for approval…")
    show("=" * 44 + "\n")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        key = link_poll(server_url, code, poll_secret)
        if key:
            show("  ✓ Bot linked.\n")
            return key
    raise TimeoutError("Link not approved in time. Restart the bot to try again.")


# ── access tokens + the pairs this account may trade ──────────────────────────
class PlanEnded(Exception):
    """The account is no longer Premium: manage open trades, open no new ones."""


class Unlinked(Exception):
    """The device key is gone or was revoked: the bot must be linked again."""


class SessionExpired(Exception):
    """The short access token was refused. Not an unlink: the next call swaps the device key for a new token."""


@dataclass
class PortalClient:
    server_url: str
    _key: str | None = None
    _token: str | None = None
    _exp: float = 0.0
    allowed_mt5: set[str] = field(default_factory=set)  # MT5 codes the account may trade, e.g. {"XAUUSD"}

    def set_key(self, key: str) -> None:
        self._key = key

    async def access_token(self, client: httpx.AsyncClient) -> str:
        """A valid access token, refreshed when within a minute of expiry. Updates allowed_mt5."""
        if self._token and time.time() < self._exp - _TOKEN_SKEW:
            return self._token
        if not self._key:
            raise Unlinked("No device key")
        r = await client.post(f"{self.server_url}/api/bot/token", headers={"x-bot-key": self._key}, timeout=15.0)
        if r.status_code == 403:
            raise PlanEnded(r.json().get("error", "Premium required"))
        if r.status_code == 401:
            raise Unlinked(r.json().get("error", "Bot not linked"))
        r.raise_for_status()
        data = r.json()
        self._token = data["accessToken"]
        self._exp = time.time() + float(data.get("expiresInSec", 900))
        self.allowed_mt5 = {p.replace("/", "").upper() for p in data.get("pairs", [])}
        return self._token

    async def fetch_signals(self, client: httpx.AsyncClient, pairs: list[str] | None = None) -> dict:
        """Latest signal per allowed pair in one request, keyed by MT5 code (XAUUSD).
        `pairs` narrows the reply to the pairs this bot trades. May raise PlanEnded/Unlinked/SessionExpired."""
        token = await self.access_token(client)
        params = {"pairs": ",".join(pairs)} if pairs else None
        r = await client.get(f"{self.server_url}/api/bot/signals", headers={"x-bot-token": token},
                             params=params, timeout=15.0)
        if r.status_code == 401:
            self._token, self._exp = None, 0.0  # token refused mid-cycle: drop it, next call re-swaps the key
            raise SessionExpired("Bot session expired")
        r.raise_for_status()
        return {k: v for k, v in r.json().get("signals", {}).items() if v}


def fetch_plan_pairs(server_url: str, device_key: str) -> list[str]:
    """The MT5 codes this account may trade right now (admin-enabled ∩ plan), for the GUI's pair checklist.
    Returns [] if the key is unlinked or the plan has lapsed."""
    with httpx.Client(base_url=server_url, timeout=15.0) as c:
        r = c.post("/api/bot/token", headers={"x-bot-key": device_key})
        if r.status_code in (401, 403):
            return []
        r.raise_for_status()
        return [p.replace("/", "").upper() for p in r.json().get("pairs", [])]


def fetch_config(server_url: str, device_key: str) -> dict:
    """Locked money rules + lot limits + the lowest client version allowed. Raises on an unlinked / non-Premium key."""
    with httpx.Client(base_url=server_url, timeout=15.0) as c:
        r = c.get("/api/bot/config", headers={"x-bot-key": device_key})
        if r.status_code == 403:
            raise PlanEnded(r.json().get("error", "Premium required"))
        if r.status_code == 401:
            raise Unlinked(r.json().get("error", "Bot not linked"))
        r.raise_for_status()
        return r.json()


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return (0,)


def version_ok(minimum: str) -> bool:
    return _version_tuple(CLIENT_VERSION) >= _version_tuple(minimum)
