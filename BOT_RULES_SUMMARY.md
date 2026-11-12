# 🤖 MT5 Trading Bot Architecture & Rules Specification

> **Purpose:** Shareable guide for developers and AI agents building or enhancing an automated MetaTrader 5 (MT5) execution bot.  
> **Source:** Production-tested configuration from `PythonMT5` (Running live on VTMarkets / Beyond Signal).

---

## 1. Core Architecture & Signal Pipeline

```
[Beyond Signal Engine] 
       │
       ▼ (Polls every 0.5s via HTTP GET /api/signals/{symbol})
[Signal Watcher & Risk Filters]
       │ 1. Check Max Signal Age (<= 30s)
       │ 2. Check Min Remaining TP Distance (>= 70%)
       │ 3. Check Daily Profit Target & Drawdown Limits
       │ 4. Re-anchor Deriv/Signal price to live Broker Tick
       ▼
[MT5 Order Execution] ──> Places Market Order (Lot size: 0.01)
       │
       ▼ (Monitors every 0.5s)
[Adaptive 5-Stage SL/TP Ladder] ──> Locks profits dynamically
```

* **Poll Interval:** `0.5 seconds` for both the signal fetcher and the trade monitor loop.
* **Authentication:** Authenticates to the signal server and protects local REST endpoints using header: `x-api-key: <API_KEY>`.

---

## 2. Symbol Selection & Strategy Edge

* **`ALLOWED_SYMBOLS=XAUUSD,BTCUSD,XAGUSD,EURUSD`**
  * **Supported Pairs:** Gold (`XAUUSD`), Bitcoin (`BTCUSD`), Silver (`XAGUSD`), and Euro (`EURUSD`).
  * **Default Volume:** `0.01` lots (Fixed conservative risk sizing for each pair).

---

## 3. Dynamic Spread Protection & Re-Anchoring

* **`TP_REDUCTION_PCT=0.10` (10% TP Pull-In):**
  * Pulls the Take Profit level 10% closer to the entry price to absorb broker spread. Ensures positions close into profit even during spread widening.
* **`SL_REDUCTION_PCT=0` (Raw SL Kept as Sent):**
  * **Rule:** Do NOT pull the Stop Loss closer to entry upon initial fill. In live backtests, tightening the initial SL caused premature spread-driven stop-outs.
* **`ANCHOR_LEVELS_TO_MARKET=true` (Critical):**
  * Remote signal feeds (e.g., Deriv or TradingView) can sit $1.00–$3.00 away from your specific broker's live tick (VTMarkets, IC Markets, etc.).
  * **Rule:** Measure the signal's *distance* in points/pips, but re-anchor the actual SL and TP prices relative to your **broker's live ask/bid tick at fill time**.

---

## 4. 📈 5-Stage Adaptive Profit Lock Ladder

Instead of static SL/TP, the bot uses an active monitor loop running every `0.5s` to trail Stop Loss into profit as price advances toward TP1:

| Price Progress (% of Move to TP1) | Bot Action Taken | Purpose |
| :--- | :--- | :--- |
| **20% to TP1** (`EARLY_RISK_CUT_TRIGGER_PCT=0.20`) | **Cut SL risk by 50%** | Reduces maximum downside early |
| **25% to TP1** (`EARLY_STEP_25_TRIGGER_PCT=0.25`) | **Lock 2% profit** (`EARLY_STEP_25_LOCK_PCT=0.02`) | Trade becomes risk-free |
| **30% to TP1** (`EARLY_STEP_30_TRIGGER_PCT=0.30`) | **Lock 5% profit** (`EARLY_STEP_30_LOCK_PCT=0.05`) | Covers commission & spread |
| **40% to TP1** (`EARLY_STEP_40_TRIGGER_PCT=0.40`) | **Lock 10% profit** (`EARLY_STEP_40_LOCK_PCT=0.10`) | Locks green baseline |
| **50% to TP1** (`EARLY_STEP_50_TRIGGER_PCT=0.50`) | **Lock 25% profit** (`EARLY_STEP_50_LOCK_PCT=0.25`) | Secures quarter target |
| **60% to TP1 (Phase 1)** (`PHASE1_TRIGGER_PCT=0.60`) | **Lock 50% profit** (`PHASE1_LOCK_PCT=0.50`) | Halfway profit guarantee |
| **TP1 Approached (Phase 2)** (`PHASE2_LOCK_PCT=0.70`) | **Lock 70% profit & Extend TP to TP2** | Lets runners run to final TP |
| **Beyond TP1** (`TRAILING_SL_PCT=0.20`) | **Trail SL by 20% of total distance** | Captures extended trend runs |

* **Noise Floor Guard (`noise_floor_atr_mult=0.6`, M1 ATR 14):**
  * Every adjusted SL must stay at least `0.6 × ATR(M1)` away from the current price. Prevents 1-minute micro-noise from stopping out trades that end up running $10–$15.

---

## 5. Circuit Breakers & Risk Management

### A. Minimum Remaining TP Distance (Chasing Filter)
* **`MIN_REMAINING_TP_ENABLED=true`**
* **`MIN_REMAINING_TP_PCT=0.70`**
* **Rule:** If the signal was issued a few seconds ago and the live price has already moved so that **less than 70% of the entry-to-TP1 distance remains**, reject the trade immediately. Never chase a move that has already run.

### B. Daily Profit Target (Greed Breaker)
* **`DAILY_PROFIT_TARGET_ENABLED=true`**
* **`DAILY_PROFIT_TARGET_USD=5.0`** / **`DAILY_PROFIT_TARGET_PCT=0.005` (0.5%)**
* **Rule:** Once the day's booked profit hits the target, halt all new trade entries until the 08:00 daily reset. Protects against giving back profits to the market.

### C. Dynamic Daily Drawdown (Capital Preservation)
* **`DAILY_DRAWDOWN_ENABLED=true`**
* **`DAILY_DRAWDOWN_PCT=0.035` (3.5%)** / **`DAILY_DRAWDOWN_MIN_USD=35.0`**
* **Rule:** If intraday loss exceeds 3.5% of the starting balance or $35 USD, trip the circuit breaker and freeze all new entries for the rest of the day.

### D. News Event Protection
* **`NEWS_CLOSE_ENABLED=true`** (`NEWS_CLOSE_MINUTES_BEFORE=5.0`)
* **Rule:** Check economic calendar for high-impact releases (CPI, NFP, FOMC). Close open positions that are in profit 5 minutes before the release.

---

## 6. How Your Friend Can Enhance Their Bot

If your friend is building an MT5 execution bot, recommend implementing these key upgrades:

1. **Implement the 5-Stage Ladder:** Static SL/TP leaves too much money on the table. Moving SL to breakeven at 25% and locking 50% profit at 60% drastically cuts drawdown.
2. **Re-Anchor Signal Prices to Broker Ticks:** Never execute the signal's raw entry/SL/TP numbers directly without adjusting for broker price discrepancies.
3. **Add the 70% Minimum Remaining TP Guard:** Eliminates the biggest source of losses: entering trades late after the impulse move has already finished.
4. **Enforce Intraday Circuit Breakers:** Automatic kill switches for daily profit and daily loss protect accounts from unexpected market volatility.
