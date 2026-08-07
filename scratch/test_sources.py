import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import asyncio
import httpx
from config import get_settings
from signal_watcher import SIGNALTRADE_PAIR_CODES, _fetch_signal
from main import app

async def test_sources():
    settings = get_settings()
    print("==================================================")
    print("1. TESTING SOURCE 1 (SignalWatcher Cloud Polling)")
    print("==================================================")
    
    source1_symbols = [p for p in SIGNALTRADE_PAIR_CODES if p in settings.allowed_symbols and p not in settings.source2_symbols]
    print(f"Source 1 Configured Symbols: {source1_symbols}")
    
    async with httpx.AsyncClient() as client:
        for symbol in source1_symbols:
            data = await _fetch_signal(client, symbol)
            if data:
                print(f"  [SUCCESS] Source 1 - {symbol}: Status 200 | Signal: {data.get('aiSignal', {}).get('signal')} | Confidence: {data.get('aiSignal', {}).get('confidence')}%")
            else:
                print(f"  [FAILED] Source 1 - {symbol}: No data returned")
                
    print("\n==================================================")
    print("2. TESTING SOURCE 2 (Local REST API `POST /trade`)")
    print("==================================================")
    
    source2_symbols = settings.source2_symbols
    print(f"Source 2 Configured Symbols: {source2_symbols}")
    
    # Test FastAPI endpoints using ASGITransport against the app instance
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = {"X-API-Key": settings.api_key}
        
        # 1. Health check
        res_health = await client.get("/health")
        print(f"  Health Check: {res_health.status_code} -> {res_health.json()}")
        
        # 2. Watcher status
        res_watcher = await client.get("/watch/status", headers=headers)
        print(f"  Watcher Status: {res_watcher.status_code} -> {res_watcher.json()}")
        
        # 3. Test POST /trade endpoint validation for Source 2 symbol (EURUSD)
        trade_payload = {
            "symbol": "EURUSD",
            "volume": 0.01,
            "order_type": "buy",
            "sl": 1.0500,
            "tp": 1.1500
        }
        res_trade = await client.post("/trade", json=trade_payload, headers=headers)
        print(f"  POST /trade Endpoint: HTTP {res_trade.status_code} -> Response: {res_trade.json()}")

if __name__ == "__main__":
    asyncio.run(test_sources())
