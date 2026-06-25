# MT5 Trade API

A FastAPI-based REST API that integrates MetaTrader 5 (MT5) with an automated signal polling system and an adaptive risk management trade monitor.

---

## Features

- **MT5 REST API**: Exposes endpoints to programmatically execute trades, check connection status, and manage active trades.
- **Automated Signal Watcher**: A background task that polls a remote `SignalTrade` endpoint for trade signals (BUY/SELL) and executes them on MT5 if they meet specific filters (e.g., confidence floors per trading session).
- **Adaptive SL/TP Monitor**: Tracks open positions (using a magic number) and modifies their Stop Loss (SL) and Take Profit (TP) levels dynamically:
  - **Initial Phase**: Moves SL to lock in partial profits (20% of TP1 distance) once price reaches 75% of the distance to TP1.
  - **TP1 Reached**: Moves SL to lock 30% profit and extends the TP level to the final target (TP2).
  - **Trailing Phase**: Trailing SL updates behind the price at a 20% distance of the total move to the final target.
- **Built-in Risk Management**:
  - **Margin Check**: Blocks new trades if margin usage exceeds 40%.
  - **Equity Peak Drawdown**: Blocks new trades if equity falls 10% below the intraday peak.
  - **Daily Loss Drawdown**: Blocks new trades if the daily loss exceeds 50%.
  - **Position Spacing Check**: Prevents duplicate executions by blocking trades if a position for the same symbol and direction is already open within proximity.

---

## Prerequisites

1. **MetaTrader 5 Terminal**: Install the desktop MT5 terminal.
2. **Windows OS**: The `MetaTrader5` python package is only compatible with Windows. If you are developing on a Mac/Linux, this service must run on a Windows machine that has MT5 installed and logged into your broker.
3. **Python 3.10+**

---

## Setup & Installation

1. **Clone the repository** (or navigate to the project directory):
   ```bash
   cd PythonMT5
   ```

2. **Create and activate a virtual environment**:
   ```bash
   # Create virtual environment
   python -m venv venv

   # Activate virtual environment
   # On Windows (cmd):
   venv\Scripts\activate
   # On Windows (PowerShell):
   .\venv\Scripts\Activate.ps1
   # On macOS/Linux (if testing other aspects):
   source venv/bin/activate
   ```

3. **Install the dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   Copy the example environment file and fill in your details:
   ```bash
   cp .env.example .env
   ```
   Edit the newly created `.env` file to configure:
   - `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` (Your MT5 broker credentials)
   - `SIGNALTRADE_URL` (IP address or URL of the SignalTrade server)
   - `API_KEY` (Security key for authorizing requests to this API)

---

## Running the Application

Before running, ensure your **MetaTrader 5 terminal** is open, running, and logged into your account.

### Using PowerShell

1. Open PowerShell and navigate to the project directory:
   ```powershell
   cd C:\Users\Administrator\Documents\GitHub\PythonMT5
   ```
2. Activate the virtual environment:
   ```powershell
   .\venv\Scripts\Activate.ps1
   ```
3. Run the application:
   ```powershell
   python main.py
   ```

### Using Command Prompt (cmd)

1. Open Command Prompt and navigate to the project directory:
   ```cmd
   cd C:\Users\Administrator\Documents\GitHub\PythonMT5
   ```
2. Activate the virtual environment:
   ```cmd
   venv\Scripts\activate.bat
   ```
3. Run the application:
   ```cmd
   python main.py
   ```

---

Once running, you can access:
- **Interactive API Documentation (Swagger UI)**: `http://localhost:8000/docs`
- **Alternative Documentation (ReDoc)**: `http://localhost:8000/redoc`

---

## API Documentation & Auth

All POST endpoints require authorization. You must supply your API key in the `X-API-Key` request header:
```http
X-API-Key: your_configured_api_key
```

### Key Endpoints

#### Health & Status
* **`GET /`**: Returns the running state.
* **`GET /health`**: Health status check.

#### Manual Trading
* **`POST /trade`**: Manually place a trade on MetaTrader 5.
  - **Payload Example**:
    ```json
    {
      "symbol": "GBPUSD",
      "volume": 0.01,
      "order_type": "buy",
      "sl": 1.25100,
      "tp": 1.25800,
      "tp1": 1.25800,
      "tp_final": 1.26500
    }
    ```

#### Signal Watcher Management
* **`POST /watch/start`**: Starts polling the remote SignalTrade endpoint for signals in the background.
* **`POST /watch/stop`**: Stops the SignalTrade background poller.
* **`GET /watch/status`**: Returns the current poller status and details of the last seen signals.

#### Trade Monitor Status
* **`GET /monitor/status`**: Retrieves information about the active SL/TP tracker background loop.
* **`GET /trades/active`**: Lists all currently tracked active trades and their progression phases (`initial`, `partial_lock`, `tp1_hit`).
