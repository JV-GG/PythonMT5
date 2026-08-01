<<<<<<< HEAD
import sys
import unittest
from unittest.mock import MagicMock, patch
import datetime

# Mock MetaTrader5 before importing project modules
mock_mt5 = MagicMock()
mock_mt5.POSITION_TYPE_BUY = 0
mock_mt5.POSITION_TYPE_SELL = 1
mock_mt5.TRADE_ACTION_SLTP = 6
mock_mt5.ORDER_TIME_GTC = 0
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.terminal_info.return_value = True

# Mock symbol info points and stops level
mock_sym_info = MagicMock()
mock_sym_info.trade_stops_level = 10
mock_sym_info.point = 0.00001
mock_mt5.symbol_info.return_value = mock_sym_info

sys.modules["MetaTrader5"] = mock_mt5

# Add parent path to sys.path
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from schemas import TradeInfo
from mt5_service import should_execute_trade
from config import get_settings

class TestMalaysiaRestrictions(unittest.TestCase):

    def setUp(self):
        mock_mt5.reset_mock()
        self.active_trades = {}
        self.settings = get_settings()

    @patch("mt5_service.datetime")
    def test_friday_malaysia_time_blocked(self, mock_dt):
        # 2026-07-24 is a Friday.
        # Let's set the base time to 12:00:00 Malaysia time (UTC+8)
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        base_dt = datetime.datetime(2026, 7, 24, 12, 0, 0, tzinfo=malaysia_tz)
        mock_dt.now.side_effect = lambda tz=None: base_dt.astimezone(tz) if tz else base_dt

        # Verify that today is Friday in the timezone
        self.assertEqual(base_dt.weekday(), 4)

        # Expected: returns False for XAUUSD because XAUUSD is blocked on Fridays
        result_xauusd = should_execute_trade("XAUUSD", "buy", 2000.0, self.active_trades)
        self.assertFalse(result_xauusd)

        # Expected: returns True for EURUSD because only XAUUSD is blocked on Fridays
        result_eurusd = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
        self.assertTrue(result_eurusd)

    @patch("mt5_service.datetime")
    def test_thursday_inside_window_allowed(self, mock_dt):
        # 2026-07-23 is a Thursday.
        # Mock time: 12:00:00 Malaysia time (inside 10:00 - 20:00)
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        base_dt = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=malaysia_tz)
        mock_dt.now.side_effect = lambda tz=None: base_dt.astimezone(tz) if tz else base_dt

        # Ensure session restrictions (like US session checks) don't block EURUSD during Asia/London
        result = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
        self.assertTrue(result)

    @patch("mt5_service.datetime")
    def test_thursday_outside_window_blocked(self, mock_dt):
        # 2026-07-23 is a Thursday.
        # Mock time: 21:00:00 Malaysia time (outside 10:00 - 20:00)
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        base_dt = datetime.datetime(2026, 7, 23, 21, 0, 0, tzinfo=malaysia_tz)
        mock_dt.now.side_effect = lambda tz=None: base_dt.astimezone(tz) if tz else base_dt

        result = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
        self.assertFalse(result)

    @patch("mt5_service.datetime")
    def test_sunday_malaysia_time_blocked(self, mock_dt):
        # 2026-07-26 is a Sunday.
        # Mock time: 12:00:00 Malaysia time
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        base_dt = datetime.datetime(2026, 7, 26, 12, 0, 0, tzinfo=malaysia_tz)
        mock_dt.now.side_effect = lambda tz=None: base_dt.astimezone(tz) if tz else base_dt

        # Sunday is outside the allowed window (or generally not traded on weekends)
        result = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
        # Should be blocked either by weekend or local time
        self.assertFalse(result)

if __name__ == "__main__":
    unittest.main()
=======
import sys
import unittest
from unittest.mock import MagicMock, patch
import datetime

# Mock MetaTrader5 before importing project modules
mock_mt5 = MagicMock()
mock_mt5.POSITION_TYPE_BUY = 0
mock_mt5.POSITION_TYPE_SELL = 1
mock_mt5.TRADE_ACTION_SLTP = 6
mock_mt5.ORDER_TIME_GTC = 0
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.terminal_info.return_value = True

# Mock symbol info points and stops level
mock_sym_info = MagicMock()
mock_sym_info.trade_stops_level = 10
mock_sym_info.point = 0.00001
mock_mt5.symbol_info.return_value = mock_sym_info

sys.modules["MetaTrader5"] = mock_mt5

# Add parent path to sys.path
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mt5_service import should_execute_trade
import signal_watcher
from config import get_settings


class TestMalaysiaRestrictions(unittest.TestCase):

    def setUp(self):
        mock_mt5.reset_mock()
        self.active_trades = {}
        self.settings = get_settings()

    def test_friday_gold_malaysia_time_blocked(self):
        """Verify XAUUSD is blocked on Friday in Malaysia Time regardless of server location."""
        signal_data = {
            "pair": "XAU/USD",
            "sessionInfo": {"name": "London", "quality": "good"},
            "aiSignal": {
                "signal": "BUY",
                "confidence": 80,
                "entry": 2400.0,
                "stopLoss": 2380.0,
                "takeProfit1": 2410.0,
                "takeProfit": 2420.0,
            }
        }

        # Mock Friday 12:00 PM Malaysia Time (UTC+8)
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        friday_malaysia = datetime.datetime(2026, 7, 24, 12, 0, 0, tzinfo=malaysia_tz)
        
        with patch("signal_watcher.datetime") as mock_dt:
            mock_dt.now.return_value = friday_malaysia
            result = signal_watcher._transform_signal(signal_data)
            self.assertIsNone(result)
            print("\nTest 1 PASS: XAUUSD Friday signal blocked in Malaysia Time (UTC+8).")

    def test_thursday_inside_window_allowed(self):
        """Verify Thursday 12:00 PM Malaysia Time is within 10:00 - 20:00 window."""
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        thursday_malaysia = datetime.datetime(2026, 7, 23, 12, 0, 0, tzinfo=malaysia_tz)

        with patch("mt5_service.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: thursday_malaysia.astimezone(tz) if tz else thursday_malaysia
            allowed = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
            self.assertTrue(allowed)
            print("Test 2 PASS: Thursday 12:00 PM Malaysia Time (UTC+8) is ALLOWED.")

    def test_thursday_outside_window_blocked(self):
        """Verify Thursday 21:00 PM Malaysia Time is outside 10:00 - 20:00 window."""
        malaysia_tz = datetime.timezone(datetime.timedelta(hours=8))
        thursday_night = datetime.datetime(2026, 7, 23, 21, 0, 0, tzinfo=malaysia_tz)

        with patch("mt5_service.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: thursday_night.astimezone(tz) if tz else thursday_night
            allowed = should_execute_trade("EURUSD", "buy", 1.08000, self.active_trades)
            self.assertFalse(allowed)
            print("Test 3 PASS: Thursday 21:00 PM Malaysia Time (UTC+8) is BLOCKED.")


if __name__ == "__main__":
    unittest.main()
>>>>>>> 36b46b442769261f38397ea583ac3354cf9f521c
