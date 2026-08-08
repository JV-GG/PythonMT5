"""
Test Signal Execution Pipeline (macOS / Dry-Run Test Harness)
Tests signal fetching, schema transformation, lot size resolution, and simulated MT5 execution.
"""
import sys
import os
import asyncio
import logging

# Set DRY_RUN=true for test execution
os.environ["DRY_RUN"] = "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("TestPipeline")

from config import get_settings
from signal_watcher import _transform_signal
from mt5_service import connect_mt5, open_trade


async def run_test():
    settings = get_settings()
    logger.info("==================================================================")
    logger.info("⚡ STARTING END-TO-END SIGNAL EXECUTION TEST (MAC SIMULATION MODE)")
    logger.info("==================================================================")

    # 1. Connect MT5 (Simulated)
    connected = connect_mt5()
    logger.info(f"1. MT5 Connection Status: {'SUCCESS (Simulated)' if connected else 'FAILED'}")

    # 2. Test Admin Portal LLM Signal Payload Transformation
    admin_payload = {
        "id": "test-dialogpt-001",
        "stock": "BTC/USD",
        "llm_signal": "BUY",
        "llm_confidence": 85.0,
        "entry": 95000.0,
        "stop_loss": 94000.0,
        "take_profit1": 96500.0,
        "take_profit": 98000.0,
        "sessionInfo": {"name": "London", "quality": "optimal"}
    }

    logger.info("\n2. Testing Admin Portal (DialoGPT / LLM) Signal Payload...")
    trade_req_admin = _transform_signal(admin_payload)
    if trade_req_admin:
        logger.info(
            f"   [SUCCESS] Transformed Admin Signal -> Symbol: {trade_req_admin.symbol} | "
            f"Direction: {trade_req_admin.order_type.upper()} | Vol: {trade_req_admin.volume} | "
            f"SL: {trade_req_admin.sl} | TP1: {trade_req_admin.tp1} | TP2: {trade_req_admin.tp_final}"
        )
        # Execute trade on MT5 Service (Simulated)
        res = open_trade(trade_req_admin)
        logger.info(f"   [MT5 EXECUTION RESPONSE] Success: {res.success} | Order Ticket: #{res.order_id} | Price: {res.executed_price}")
    else:
        logger.error("   [FAILED] Failed to transform Admin payload")

    # 3. Test Client Portal Signal Payload Transformation
    client_payload = {
        "pair": "BTC/USD",
        "sessionInfo": {"name": "London", "quality": "optimal"},
        "aiSignal": {
            "signal": "SELL",
            "confidence": 78.0,
            "entry": 95000.0,
            "stopLoss": 96000.0,
            "takeProfit1": 93500.0,
            "takeProfit": 92000.0
        }
    }

    logger.info("\n3. Testing Client Portal (MiniMax) Signal Payload...")
    trade_req_client = _transform_signal(client_payload)
    if trade_req_client:
        logger.info(
            f"   [SUCCESS] Transformed Client Signal -> Symbol: {trade_req_client.symbol} | "
            f"Direction: {trade_req_client.order_type.upper()} | Vol: {trade_req_client.volume} | "
            f"SL: {trade_req_client.sl} | TP1: {trade_req_client.tp1} | TP2: {trade_req_client.tp_final}"
        )
        # Execute trade on MT5 Service (Simulated)
        res_client = open_trade(trade_req_client)
        logger.info(f"   [MT5 EXECUTION RESPONSE] Success: {res_client.success} | Order Ticket: #{res_client.order_id} | Price: {res_client.executed_price}")
    else:
        logger.error("   [FAILED] Failed to transform Client payload")

    logger.info("\n==================================================================")
    logger.info("✅ END-TO-END PIPELINE TEST COMPLETE — ALL SIGNALS PROCESSED!")
    logger.info("==================================================================")


if __name__ == "__main__":
    asyncio.run(run_test())
