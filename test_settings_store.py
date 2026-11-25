"""Self-contained checks for settings_store (no keyring, no MT5). Run: python3 test_settings_store.py"""
import settings_store as ss


def test_clamp_lot():
    assert ss.clamp_lot("0.05") == 0.05
    assert ss.clamp_lot("5") == 1.0          # above max
    assert ss.clamp_lot("0") == 0.01         # below min
    assert ss.clamp_lot("abc") == 0.01       # not a number
    assert ss.clamp_lot("") == 0.01
    assert ss.clamp_lot("0.5", 0.1, 0.3) == 0.3  # custom clamp
    assert ss.clamp_lot(None) == 0.01


def test_valid_account():
    assert ss.valid_account("51882204") is True
    assert ss.valid_account(" 12345 ") is True
    assert ss.valid_account("abc") is False
    assert ss.valid_account("") is False
    assert ss.valid_account("12.5") is False


def test_pairs_to_trade():
    allowed = ["XAUUSD", "BTCUSD", "ETHUSD"]
    assert ss.pairs_to_trade([], allowed) == allowed                 # none ticked -> all allowed
    assert ss.pairs_to_trade(["BTCUSD"], allowed) == ["BTCUSD"]      # kept
    assert ss.pairs_to_trade(["XAG/USD"], allowed) == []             # ticked but not allowed -> dropped
    assert ss.pairs_to_trade(["btc/usd"], allowed) == ["BTCUSD"]     # case / slash normalised
    assert ss.pairs_to_trade(["ETHUSD", "XAUUSD"], allowed) == ["XAUUSD", "ETHUSD"]  # allowed order kept


def test_roundtrip_file(tmp_path, monkeypatch):
    # force the file fallback (no keyring backend)
    monkeypatch.setattr(ss, "_keyring", lambda: None)
    monkeypatch.setattr(ss, "_settings_path", lambda: str(tmp_path / "s.json"))
    ss.save_settings({"mt5_login": "123", "mt5_password": "p", "mt5_server": "X", "lot_size": 0.05,
                      "pairs": ["XAUUSD"], "ignored": "x"})
    got = ss.load_settings()
    assert got["mt5_login"] == "123"
    assert got["pairs"] == ["XAUUSD"]
    assert "ignored" not in got  # only known FIELDS persisted


if __name__ == "__main__":
    test_clamp_lot()
    test_valid_account()
    test_pairs_to_trade()
    # tiny stand-ins so the file runs without pytest
    import tempfile, types
    with tempfile.TemporaryDirectory() as d:
        mp = types.SimpleNamespace(setattr=lambda o, n, v: setattr(o, n, v))
        test_roundtrip_file(__import__("pathlib").Path(d), mp)
    print("test_settings_store: all checks passed")
