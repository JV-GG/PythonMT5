"""
Turn the bot's text logs into clean CSV tables for analysis. READ-ONLY: never sends orders.

Inputs  : logs/archive/*.log.gz and trading.log (overlapping lines are skipped by timestamp)
Outputs : logs/extracted/
    signals.csv   one row per "New BUY/SELL signal", with what happened to it (executed / blocked + reason)
    trades.csv    one row per filled trade: entry, fill, levels, SL-move count, exit (from MT5 deal history
                  when the position belongs to the currently logged-in account)
    sl_moves.csv  one row per SL/TP change made by the trade monitor
    outages.csv   periods when the SignalTrade feed was unreachable
    daily.csv     per trading day: start balance, when the profit target / drawdown breaker fired

Usage: venv\\Scripts\\python.exe tools\\log_extract.py [--no-mt5]
"""
import csv, glob, gzip, os, re, sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "extracted")
OUTAGE_GAP = timedelta(minutes=5)   # errors closer than this belong to the same outage

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \| (\w+) \| ([\w.]+) \| (.*)$")
NUM = r"(-?[\d.]+)"

R_SIGNAL = re.compile(r"New (BUY|SELL) signal for (\S+) \| session=(.+?) \((\w+)\) \| confidence=([\d.]+)% \| sl=" + NUM + r" tp=" + NUM)
R_CHECK = re.compile(r"TP1 distance check (passed|.*?) for (\S+) (BUY|SELL) .*Signal Entry=" + NUM)
R_TP_BLOCK = re.compile(r"Trade blocked \(Insufficient TP1 distance\) for (\S+) (BUY|SELL) \| Only (-?[\d.]+)%.*Signal Entry=" + NUM)
R_BLOCK = re.compile(r"Trade (?:blocked|skipped) \((.+?)\) for (\S+) \|")
R_DETAIL = re.compile(r"Trade blocked \((local time restrictions|session restrictions)\)|Trade skipped: Symbol \S+ has (\d+) active (?:(BUY|SELL) )?positions")
R_EXEC = re.compile(r"Executing trade \| symbol=(\S+) volume=([\d.]+) type=(buy|sell) price=" + NUM + r" sl=" + NUM + r" tp=" + NUM + r" comment=(\S*)")
R_FILLED = re.compile(r"Trade executed \| order_id=(\d+) price=" + NUM + r" volume=([\d.]+)")
R_REG = re.compile(r"Trade registered \| order_id=(\d+) symbol=(\S+) direction=(buy|sell) entry=" + NUM + r" tp1=" + NUM + r" tp2=(\S+)")
R_FAIL = re.compile(r"Failed to fire trade for (\S+): (.*)")
R_MON = re.compile(r"^\[(\d+)\] (?:(\S+) (BUY|SELL) \| )?(.+?) \| (.*)$")
R_REMOVED = re.compile(r"^\[(\d+)\] Removed from monitor")
R_CLOSED = re.compile(r"exit=" + NUM + r" reason=(\S+) profit=" + NUM + r" move=" + NUM + r" last_phase=(\S+)(?: held=([\d.]+)m)?")
R_FEED_ERR = re.compile(r"Failed to reach SignalTrade at (https?://[^/]+)|SignalTrade returned status (\d+)")
R_RESET = re.compile(r"Daily metrics reset for (\S+) .*starting_balance=" + NUM)
R_PROFIT_HIT = re.compile(r"Daily profit target reached .*closed_profit=" + NUM)
R_DD_HIT = re.compile(r"Daily drawdown limit reached \| loss=\$" + NUM)


def mon_numbers(text):
    sl = re.search(r"(?:locked_sl|applied_sl|new_sl)=" + NUM, text) or re.search(r"Entry \(?" + NUM, text) or re.search(r"\(" + NUM + r"\)", text)
    tp = re.search(r"(?:new_tp|tp_final)=" + NUM, text)
    px = re.search(r"price[= ]" + NUM, text)
    return (float(sl.group(1)) if sl else None, float(tp.group(1)) if tp else None, float(px.group(1)) if px else None)


def mon_stage(event):
    e = event.lower()
    for key, stage in (("risk reduction", "20%_risk_cut"), ("early 25%", "25%"), ("early 30%", "30%"), ("early 40%", "40%"),
                       ("early 50%", "50%"), ("phase 1 lock", "60%_phase1"), ("tp1 hit", "tp1_hit"), ("trailing", "trailing")):
        if key in e:
            return stage
    return None


STAGE_ORDER = ["20%_risk_cut", "25%", "30%", "40%", "50%", "60%_phase1", "tp1_hit", "trailing"]


def read_lines():
    files = sorted(glob.glob(os.path.join(ROOT, "logs", "archive", "*.log.gz")))
    live = os.path.join(ROOT, "trading.log")
    if os.path.exists(live):
        files.append(live)
    last_ts = ""
    for path in files:
        opener = gzip.open if path.endswith(".gz") else open
        file_max = last_ts
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = LINE.match(raw.rstrip("\n"))
                if not m or m.group(1) <= last_ts:
                    continue
                file_max = max(file_max, m.group(1))
                yield m.groups()
        last_ts = file_max


def mt5_exits(order_ids):
    """Exit details from MT5 deal history (only for positions of the logged-in account)."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {}
    if not mt5.initialize():
        return {}
    reasons = {0: "client", 1: "mobile", 2: "web", 3: "bot", 4: "SL", 5: "TP", 6: "stop_out"}
    out = {}
    deals = mt5.history_deals_get(datetime(2020, 1, 1), datetime.now() + timedelta(days=1)) or []
    for d in deals:
        if d.position_id in order_ids and d.entry == mt5.DEAL_ENTRY_OUT:
            out[d.position_id] = dict(exit_server_time=datetime.utcfromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S"),
                                      exit_price=d.price, exit_reason=reasons.get(d.reason, str(d.reason)),
                                      profit=d.profit, commission=d.commission, swap=d.swap)
    mt5.shutdown()
    return out


def main():
    use_mt5 = "--no-mt5" not in sys.argv
    signals, trades, moves, outages, daily = [], {}, [], [], {}
    sig = None                # signal currently being processed by the watcher
    pending_exec = None       # "Executing trade" waiting for its fill / registration
    last_detail = None        # mt5_service detail that explains the next "skipped (spacing)"
    outage = None

    for ts, level, src, msg in read_lines():
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

        # ── Feed outages ──────────────────────────────────────────────────
        if (m := R_FEED_ERR.search(msg)):
            what = m.group(1) or f"HTTP {m.group(2)}"
            if outage and t - outage["_last"] <= OUTAGE_GAP:
                outage["_last"], outage["errors"] = t, outage["errors"] + 1
            else:
                outage = dict(start=ts, _last=t, errors=1, kind=what)
                outages.append(outage)
            continue

        # ── Daily circuit breakers ────────────────────────────────────────
        if (m := R_RESET.search(msg)):
            daily.setdefault(m.group(1), {"date": m.group(1)})["start_balance"] = float(m.group(2))
            continue
        if (m := R_PROFIT_HIT.search(msg)):
            d = daily.setdefault(ts[:10], {"date": ts[:10]})
            d.setdefault("profit_target_hit_at", ts[11:]); d.setdefault("closed_profit_at_hit", float(m.group(1)))
            continue
        if (m := R_DD_HIT.search(msg)):
            d = daily.setdefault(ts[:10], {"date": ts[:10]})
            d.setdefault("drawdown_hit_at", ts[11:]); d.setdefault("loss_at_hit", float(m.group(1)))
            continue

        # ── Signals and what happened to them ─────────────────────────────
        if (m := R_SIGNAL.search(msg)):
            sig = dict(time=ts, pair=m.group(2), dir=m.group(1).lower(), session=m.group(3), session_quality=m.group(4),
                       confidence=float(m.group(5)), sl_sent=float(m.group(6)), tp1_sent=float(m.group(7)),
                       signal_entry=None, outcome="", reason="", order_id="")
            prev = next((s for s in reversed(signals) if s["pair"] == sig["pair"]), None)
            sig["repeat_of_previous"] = bool(prev and all(prev[k] == sig[k] for k in ("dir", "confidence", "sl_sent", "tp1_sent")))
            signals.append(sig)
            last_detail = None
            continue
        if sig and not sig["outcome"]:
            if (m := R_CHECK.search(msg)) or (m := R_TP_BLOCK.search(msg)):
                sig["signal_entry"] = float(m.group(4))
                if m.re is R_TP_BLOCK:
                    sig["tp_distance_left_pct"] = float(m.group(3))
            if (m := R_DETAIL.search(msg)):
                last_detail = m.group(1) or f"max {m.group(3) or 'total'} positions ({m.group(2)} open)"
            if (m := R_BLOCK.search(msg)) and m.group(2) == sig["pair"]:
                sig["outcome"] = "blocked"
                sig["reason"] = (last_detail if m.group(1) == "spacing" and last_detail else m.group(1))
                continue
            if (m := R_FAIL.search(msg)) and m.group(1) == sig["pair"]:
                sig["outcome"], sig["reason"] = "rejected", m.group(2)[:120]
                continue

        # ── Trade execution ───────────────────────────────────────────────
        if (m := R_EXEC.search(msg)):
            pending_exec = dict(time=ts, symbol=m.group(1), volume=float(m.group(2)), dir=m.group(3), req_price=float(m.group(4)),
                                sl=float(m.group(5)), tp1=float(m.group(6)), confidence=m.group(7).rstrip("%"),
                                signal_time=sig["time"] if sig and not sig["outcome"] else "",
                                signal_entry=sig["signal_entry"] if sig and not sig["outcome"] else None)
            continue
        if (m := R_FILLED.search(msg)) and pending_exec:
            pending_exec.update(order_id=int(m.group(1)), fill_price=float(m.group(2)))
            continue
        if (m := R_REG.search(msg)):
            oid = int(m.group(1))
            tr = pending_exec if pending_exec and pending_exec.get("order_id") == oid else dict(time=ts, symbol=m.group(2), dir=m.group(3))
            tr.update(order_id=oid, fill_price=float(m.group(4)), tp1=float(m.group(5)),
                      tp2=None if m.group(6) == "None" else float(m.group(6)), sl_moves=0, last_stage="", closed_seen="")
            if tr.get("signal_entry") is not None:
                d = 1 if tr["dir"] == "buy" else -1
                tr["entry_vs_signal"] = round(d * (tr["fill_price"] - tr["signal_entry"]), 6)   # >0 = filled worse than signal
            if tr.get("req_price") is not None:
                tr["slippage"] = round((1 if tr["dir"] == "buy" else -1) * (tr["fill_price"] - tr["req_price"]), 6)
            trades[oid] = tr
            if sig and not sig["outcome"]:
                sig["outcome"], sig["order_id"] = "executed", oid
            pending_exec = None
            continue

        # ── Trade monitor ─────────────────────────────────────────────────
        if src == "trade_monitor":
            if (m := R_REMOVED.match(msg)):
                tr = trades.get(int(m.group(1)))
                if tr is not None:
                    tr["closed_seen"] = ts
                    if (c := R_CLOSED.search(msg)):   # newer bot versions log the exit details themselves
                        tr.update(exit_price=float(c.group(1)), exit_reason=c.group(2), profit=float(c.group(3)),
                                  exit_move=float(c.group(4)), held_minutes=float(c.group(6)) if c.group(6) else None)
                continue
            m = R_MON.match(msg)
            if not m or "Resilient Profit Lock" in msg or "MT5 rejected" in msg or "failed" in msg.lower():
                continue
            oid = int(m.group(1))
            stage = mon_stage(m.group(4))
            if stage is None:
                continue
            sl, tp, px = mon_numbers(m.group(5) if "|" not in m.group(4) else msg)
            tr = trades.get(oid, {})
            moves.append(dict(time=ts, order_id=oid, symbol=m.group(2) or tr.get("symbol", ""), dir=(m.group(3) or tr.get("dir", "")).lower(),
                              stage=stage, new_sl=sl, new_tp=tp, price=px))
            if tr:
                tr["sl_moves"] += 1
                if not tr["last_stage"] or STAGE_ORDER.index(stage) >= STAGE_ORDER.index(tr["last_stage"]):
                    tr["last_stage"] = stage

    for o in outages:
        o["end"] = o.pop("_last").strftime("%Y-%m-%d %H:%M:%S")
        o["minutes"] = round((datetime.strptime(o["end"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(o["start"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60, 1)

    if use_mt5:
        for oid, ex in mt5_exits(set(trades)).items():
            tr = trades[oid]
            tr.update(ex)
            d = 1 if tr["dir"] == "buy" else -1
            tr["exit_move"] = round(d * (ex["exit_price"] - tr["fill_price"]), 6)

    os.makedirs(OUT, exist_ok=True)

    def write(name, rows):
        if not rows:
            return
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print(f"  {name:13s} {len(rows):6d} rows")

    for s in signals:
        if not s["outcome"]:
            s["outcome"] = "no_decision_logged"
    print(f"Wrote to {OUT}:")
    write("signals.csv", signals)
    write("trades.csv", list(trades.values()))
    write("sl_moves.csv", moves)
    write("outages.csv", outages)
    write("daily.csv", sorted(daily.values(), key=lambda d: d["date"]))


if __name__ == "__main__":
    main()
