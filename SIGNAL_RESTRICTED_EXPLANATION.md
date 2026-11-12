# Beyond Signal API — Why Does It Say `"restricted": true`?

This document explains why the Beyond Signal API returns `"restricted": true` and `"signal": "NEUTRAL"`, what it means, and how to resolve it.

---

## 1. Quick Answer (TL;DR)

1. **`"restricted": true` is NOT an error or server failure.**
   It is the system's **Subscription Plan Gating**. The account making this request is on the **Free Tier**.
   * **Free Tier**: Restricted to **Gold (`XAU/USD`) only**. That is why `"count": 1` and only Gold is returned.
   * **Pro / VIP Tier**: Has access to **all symbols** (`all_symbols`), which returns `"restricted": false` with all Forex and Crypto pairs (`EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `BTCUSD`, etc.).

2. **Why `"signal": "NEUTRAL"` and `entry: 0, tp: 0, sl: 0`?**
   * The system did not find a high-probability trade entry at that moment.
   * The multi-timeframe trend gate was flat (`"anchor": "FLAT", "trigger": "FLAT"`).
   * Rather than forcing a bad trade, the engine waits for a clear setup.
   * Notice `"lastSignal"`: the previous signal was a **`BUY`** that achieved **`WIN`** (Take Profit target hit). The engine is currently waiting for the next valid setup.

---

## 2. Detailed Breakdown of the Response JSON

```json
{
  "signals": [
    {
      "symbol": "XAU/USD",
      "symbolCode": "XAUUSD",
      "signal": "NEUTRAL",
      "entry": 0, "tp1": 0, "tp": 0, "sl": 0,
      "confidence": 0,
      "trendGate": { "allowed": null, "anchor": "FLAT", "primary": "DOWN", "trigger": "FLAT" },
      "lastSignal": { "signal": "BUY", "outcome": "WIN", "outcomeTarget": "TP" }
    }
  ],
  "count": 1,
  "restricted": true,
  "source": "cache"
}
```

| Field | Value | Meaning |
| :--- | :--- | :--- |
| **`restricted`** | `true` | The requesting user does not have the `all_symbols` permission. Only Gold is visible. |
| **`count`** | `1` | Only 1 pair is returned because all other currency/crypto pairs are filtered out. |
| **`signal`** | `NEUTRAL` | Market conditions do not meet entry criteria; no active BUY or SELL order. |
| **`entry / sl / tp`** | `0` | No active position levels because `signal` is NEUTRAL. |
| **`trendGate`** | `anchor: FLAT` | Multi-timeframe trend filter indicates sideways/choppy market; trading blocked to protect capital. |
| **`lastSignal`** | `WIN` | The most recent completed trade hit its Take Profit. |
| **`source`** | `cache` | Response served rapidly from Redis cache. |

---

## 3. How the Backend Code Handles This

In the backend route (`app/api/symbols/route.ts`):

```typescript
// 1. Check if the user has access to all symbols or only XAU/USD
const symbolsAccess = await checkAccess(request, 'symbols_xauusd');
const allAccess = await checkAccess(request, 'all_symbols');

// 2. Filter allowed pairs based on user plan
const allowedPairs = showAllSymbols
  ? activePairs                           // Pro/VIP: All currency & crypto pairs
  : activePairs.filter(p => p.pair === 'XAU/USD'); // Free: Gold only

// 3. Return response with restriction flag
return NextResponse.json({
  signals: signalsWithMeta,
  count: signalsWithMeta.length,
  restricted: !showAllSymbols,           // true if user is on Free Tier
  source: isSourceCache ? 'cache' : 'mixed',
});
```

---

## 4. How to Resolve It / Unlock All Symbols

### Solution A: Upgrade Account Plan (Recommended)
* In the Beyond Signal web dashboard, upgrade the account to **Pro** or **VIP**.
* Paid plans grant the `all_symbols` entitlement, which will automatically return:
  * `"restricted": false`
  * `"count": 6` (or total active pairs)
  * Full signal data for `EUR/USD`, `GBP/USD`, `USD/JPY`, `AUD/USD`, `BTC/USD`, and `XAU/USD`.

### Solution B: Admin Database Update
If managing the backend database directly, update the user's plan:
```sql
-- Update the user's plan in PostgreSQL
UPDATE users 
SET plan = 'pro' 
WHERE email = 'user_email@example.com';
```
Or temporarily disable subscription gating globally:
```sql
UPDATE subscription_settings 
SET value = 'false' 
WHERE key = 'master_toggle';
```

---

## 5. Note for Automated Bots (PythonMT5)

If connecting an automated trading bot (e.g. `PythonMT5`):
* **Do NOT use the frontend `/api/symbols` endpoint.** That endpoint is designed for web users with session cookies and enforces plan restrictions.
* **Use the Direct Signal API with your API Key:**
  ```http
  GET https://signal.beyond-signal.net/api/signals/XAUUSD
  Header: x-api-key: your_api_key_here
  ```
  Or for other pairs:
  ```http
  GET https://signal.beyond-signal.net/api/signals/EURUSD
  GET https://signal.beyond-signal.net/api/signals/BTCUSD
  ```
  The direct `/api/signals/{pair}` endpoint authenticates via API key and delivers the raw trade signal directly to your bot.
