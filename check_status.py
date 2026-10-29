"""
Quick CLI status checker — shows whether trading is ACTIVE or PAUSED today,
current daily closed profit vs. daily profit target, drawdown vs. limit,
margin usage, and open MT5 positions.

Usage:
    .\\venv\\Scripts\\python.exe check_status.py
"""
import logging
import MetaTrader5 as mt5
from config import get_settings
import mt5_service as ms

logging.disable(logging.CRITICAL)


def main() -> None:
    s = get_settings()
    ms.connect_mt5()
    try:
        margin_ok, info = ms.is_margin_safe()
        dd_ok, _ = ms.is_drawdown_safe()
        pt_ok, _ = ms.is_daily_profit_target_safe()
        closed_pnl = ms._get_today_closed_profit()

        start_bal = ms._daily_start_balance or (info["balance"] if info else 0.0)
        peak_eq = ms._peak_equity or (info["equity"] if info else 0.0)
        baseline = max(start_bal, peak_eq)

        target_pct_val = start_bal * s.daily_profit_target_pct
        effective_profit_target = min(s.daily_profit_target_usd, target_pct_val) if target_pct_val > 0 else s.daily_profit_target_usd
        remaining_to_target = max(effective_profit_target - closed_pnl, 0.0)

        allowed_loss = max(baseline * s.daily_drawdown_pct, s.daily_drawdown_min_usd)
        floating_pnl = (info["equity"] - info["balance"]) if info else 0.0
        total_pnl = closed_pnl + floating_pnl
        current_loss = max(-total_pnl if total_pnl < 0 else 0.0, (baseline - info["equity"]) if (info and baseline > info["equity"]) else 0.0)

        positions = mt5.positions_get() if mt5 is not None else []
        pos_count = len(positions) if positions else 0

        can_trade = margin_ok and dd_ok and pt_ok
        status_str = "ACTIVE (Will Continue Trading)" if can_trade else "PAUSED (Stopped Until Next 08:00 Session)"

        print("=" * 60)
        print(f"  TRADING STATUS : {status_str}")
        print(f"  Session Date   : {ms._last_reset_date} (Resets daily at {s.local_time_start})")
        print("=" * 60)
        if info:
            total_m = info["margin"] + info["free_margin"]
            m_pct = (info["margin"] / total_m * 100) if total_m > 0 else 0.0
            print(f"  Start Balance  : {start_bal:.2f} USC")
            print(f"  Current Balance: {info['balance']:.2f} USC | Equity: {info['equity']:.2f} USC")
            print(f"  Margin Usage   : {m_pct:.2f}% (Limit: 40.00%) -> {'OK' if margin_ok else 'BLOCKED'}")
        print("-" * 60)
        print(
            f"  Daily Profit   : {closed_pnl:+.2f} USC / {effective_profit_target:.2f} USC target "
            f"(USD cap: {s.daily_profit_target_usd:.2f}, {s.daily_profit_target_pct*100:.2f}% cap: {target_pct_val:.2f})"
        )
        if pt_ok:
            print(f"  Profit Status  : NOT REACHED ({remaining_to_target:.2f} USC more to hit daily target)")
        else:
            print("  Profit Status  : TARGET REACHED (Trading paused for today)")
        print("-" * 60)
        print(f"  Daily Drawdown : {current_loss:.2f} USC / {allowed_loss:.2f} USC limit -> {'OK' if dd_ok else 'HIT (Paused)'}")
        print(f"  Open Positions : {pos_count}")
        print("=" * 60)
    finally:
        ms.disconnect_mt5()


if __name__ == "__main__":
    main()

