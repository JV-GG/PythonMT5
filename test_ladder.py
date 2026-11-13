"""Self-check of the profit-lock ladder's Phase 2 (near / past TP1, TP trailing). Run: python test_ladder.py"""
from types import SimpleNamespace

import trade_monitor as tm
from schemas import PHASE_INITIAL, PHASE_PARTIAL_LOCK, PHASE_TP1_HIT, TradeInfo

sent: list[tuple[str, float, float]] = []
price = 0.0
tm.get_settings = lambda: SimpleNamespace(
    noise_floor_atr_mult=0.0, early_risk_reduction_enabled=False, phase1_trigger_pct=0.60, phase1_lock_pct=0.50,
    phase2_lock_pct=0.70, trailing_sl_pct=0.20, monitor_tp1_proximity_pct=0.10,
)
tm._get_tick = lambda s: {"bid": price, "ask": price}
tm._modify_position = lambda t, s, d, sl, tp, csl, ctp, stage="", price=None: (sent.append((stage, round(sl, 2), round(tp, 2))), (True, sl, tp))[1]
tm.save_active_trades = lambda: None


def step(trade: TradeInfo, at: float) -> tuple[str, float, float] | None:
    global price
    price = at
    sent.clear()
    tm._process_one_position({"ticket": trade.order_id, "symbol": "XAUUSD", "type": trade.direction, "sl": trade.current_sl,
                              "tp": trade.current_tp, "price_open": trade.entry_price})
    return sent[-1] if sent else None


# BUY 2000, SL 1982, TP1 2018, TP2 2036
buy = TradeInfo(order_id=1, symbol="XAUUSD", direction="buy", entry_price=2000.0, initial_sl=1982.0, current_sl=1982.0,
                initial_tp1=2018.0, current_tp=2018.0, tp2=2036.0, phase=PHASE_INITIAL)
tm.active_trades[1] = buy

# 65% of the way: Phase 1 lock only. The old fixed 5-pip "near TP1" fired here on forex
step(buy, 2011.7)
assert buy.phase == PHASE_PARTIAL_LOCK, buy.phase

# Inside the last 10% of TP1: Phase 2 locks 70% and moves the TP to TP2
assert step(buy, 2016.5) == ("tp1_hit", 2012.6, 2036.0), sent
assert buy.phase == PHASE_TP1_HIT

# TP2 still more than half a step (18) away: SL to TP1, the TP is not touched
assert step(buy, 2020.0) == ("tp1_trail", 2018.0, 2036.0), sent

# Within half a step of TP2: the TP moves one step (18) ahead of price, SL trails 20% of the TP2 distance behind
assert step(buy, 2030.0) == ("tp1_trail", 2022.8, 2048.0), sent

# SELL mirror: 2000, TP1 1982, TP2 1964, already in Phase 2
sell = TradeInfo(order_id=2, symbol="XAUUSD", direction="sell", entry_price=2000.0, initial_sl=2018.0, current_sl=1987.4,
                 initial_tp1=1982.0, current_tp=1964.0, tp2=1964.0, phase=PHASE_TP1_HIT)
tm.active_trades[2] = sell
assert step(sell, 1980.0) == ("tp1_trail", 1982.0, 1964.0), sent
assert step(sell, 1970.0) == ("tp1_trail", 1977.2, 1952.0), sent
print("ok")
