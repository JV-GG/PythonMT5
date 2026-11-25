"""
Remember the customer's GUI settings between launches.

Stored in the Windows Credential Manager via keyring (same place as the device key), so the MT5
password is not left in a plain file; falls back to a 0600 file under ~/.beyondsignal when keyring is
unavailable. The customer chose to have their MT5 login remembered so they only click Start next time.
"""
from __future__ import annotations

import json
import os

from portal_link import _keyring  # reuse the same credential backend as the device key

_SERVICE = "BeyondSignalBot"
_ACCOUNT = "gui_settings"
FIELDS = ("mt5_login", "mt5_password", "mt5_server", "lot_size", "pairs")


def _settings_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".beyondsignal")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "settings.json")


def load_settings() -> dict:
    raw = None
    kr = _keyring()
    if kr is not None:
        try:
            raw = kr.get_password(_SERVICE, _ACCOUNT)
        except Exception:
            raw = None
    if raw is None:
        p = _settings_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                raw = f.read()
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    raw = json.dumps({k: data.get(k) for k in FIELDS})
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, _ACCOUNT, raw)
            return
        except Exception:
            pass
    p = _settings_path()
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


# ── pure helpers (unit-tested) ────────────────────────────────────────────────
def clamp_lot(value, lo: float = 0.01, hi: float = 1.0) -> float:
    """A valid lot inside [lo, hi]; bad input falls back to the minimum."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    if v != v:  # NaN
        return lo
    return max(lo, min(hi, v))


def valid_account(value) -> bool:
    """MT5 login is an all-digits account number."""
    return bool(str(value).strip()) and str(value).strip().isdigit()


def pairs_to_trade(selected, allowed) -> list[str]:
    """The pairs the bot should trade: the customer's ticks kept to what the plan allows, order preserved.
    Empty selection means 'all allowed'."""
    allowed_up = [p.replace("/", "").upper() for p in allowed]
    if not selected:
        return list(allowed_up)
    sel = {p.replace("/", "").upper() for p in selected}
    return [p for p in allowed_up if p in sel]
