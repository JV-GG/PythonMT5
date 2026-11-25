"""Self-contained checks for portal_link.PortalClient (no server, no MT5). Run: python3 test_portal_link.py"""
import asyncio
import httpx

from portal_link import PortalClient, PlanEnded, Unlinked


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run():
    calls = {"n": 0}

    # 1. token is fetched once, cached, and allowed_mt5 parsed from display pairs
    def ok(req):
        calls["n"] += 1
        assert req.headers["x-bot-key"] == "bs_bot_abc"
        return httpx.Response(200, json={"accessToken": "tok1", "expiresInSec": 900, "pairs": ["XAU/USD", "BTC/USD"]})
    p = PortalClient(server_url="https://s", _key="bs_bot_abc")
    async with client_for(ok) as c:
        assert await p.access_token(c) == "tok1"
        assert await p.access_token(c) == "tok1"   # served from cache
    assert calls["n"] == 1, calls
    assert p.allowed_mt5 == {"XAUUSD", "BTCUSD"}, p.allowed_mt5

    # 2. a near-expired token is refreshed
    p._exp = 0
    def ok2(req):
        calls["n"] += 1
        return httpx.Response(200, json={"accessToken": "tok2", "expiresInSec": 900, "pairs": ["XAU/USD"]})
    async with client_for(ok2) as c:
        assert await p.access_token(c) == "tok2"
    assert calls["n"] == 2
    assert p.allowed_mt5 == {"XAUUSD"}

    # 3. plan ended -> PlanEnded
    p._exp = 0
    async with client_for(lambda r: httpx.Response(403, json={"error": "Premium required", "code": "plan_ended"})) as c:
        try:
            await p.access_token(c); assert False, "expected PlanEnded"
        except PlanEnded:
            pass

    # 4. revoked / unlinked -> Unlinked
    p._exp = 0
    async with client_for(lambda r: httpx.Response(401, json={"error": "unlinked"})) as c:
        try:
            await p.access_token(c); assert False, "expected Unlinked"
        except Unlinked:
            pass

    # 5. no key at all -> Unlinked without a request
    q = PortalClient(server_url="https://s")
    async with client_for(lambda r: httpx.Response(500)) as c:
        try:
            await q.access_token(c); assert False, "expected Unlinked"
        except Unlinked:
            pass

    print("test_portal_link: all 5 checks passed")


if __name__ == "__main__":
    asyncio.run(run())
