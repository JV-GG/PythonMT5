# Fix: "Start trading" hangs on "Starting…" forever

## Symptom
After the bot is linked and MT5 is connected, clicking **Start trading** shows
`Starting…` and never flips to `● Running`. No error dialog appears. It stays stuck
for 15+ minutes. Nothing trades.

## Root cause
The trading engine is **asyncio-based**: `signal_watcher._watcher_loop()` and
`trade_monitor._monitor_loop()` are `async def`, and they are launched with
`asyncio.create_task(...)` inside `start_watcher()` / `start_monitor()`.

`asyncio.create_task()` only works **inside a running event loop**.

- `main.py` (the FastAPI server) has an event loop, so it works there.
- `gui.py` (the desktop app) is Tkinter — it has **no asyncio event loop**, and
  `_start_worker` runs on a plain `threading.Thread`. So `start_monitor()` /
  `start_watcher()` raise `RuntimeError: no running event loop`, the start sequence
  never finishes, and the UI never leaves "Starting…".

Proof (same call the engine makes, in a thread with no loop):
```
RuntimeError: no running event loop
```

## The fix (file: `gui.py`)
Run a dedicated asyncio event loop in a background thread for the engine's lifetime,
and start/stop the watcher & monitor **on that loop** (not on the worker thread).
All MT5 access then happens on one thread, which is also safer.

### 1. Import asyncio (top of file, with the other imports)
```python
import asyncio
```

### 2. In `BotApp.__init__` — start the engine loop (after `self._link = None`)
```python
        # The trading engine (signal watcher + trade monitor) is asyncio-based and needs a LIVE
        # event loop. A Tk GUI has none, so we run one in a background thread for the engine.
        # Without it, start_watcher()/start_monitor() raise "no running event loop" and
        # "Start trading" hangs forever on "Starting…".
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
```

### 3. Add the loop runner method (e.g. just above `def _start_worker`)
```python
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
```

### 4. In `_start_worker` — replace the direct connect/start calls
Replace:
```python
            connect_mt5()
            load_active_trades()
            start_monitor()
            start_watcher()
            self.root.after(0, self._started_ok)
```
with:
```python
            # Connect to MT5, load state and launch the watcher/monitor ON the engine loop, so
            # the async tasks have a running loop and every MT5 call stays on one thread. result()
            # waits here, so a connect error or hang surfaces as a clear "Could not start" instead
            # of leaving the UI stuck on "Starting…".
            async def _begin():
                connect_mt5()
                load_active_trades()
                start_monitor()
                start_watcher()

            asyncio.run_coroutine_threadsafe(_begin(), self.loop).result(timeout=60)
            self.root.after(0, self._started_ok)
```

### 5. In `_stop` — stop on the engine loop
Replace:
```python
            stop_watcher()
            stop_monitor()
            disconnect_mt5()
```
with:
```python
            # Cancel the tasks and disconnect on the engine loop (same thread they run on).
            async def _end():
                stop_watcher()
                stop_monitor()
                disconnect_mt5()

            asyncio.run_coroutine_threadsafe(_end(), self.loop).result(timeout=15)
```

### 6. In `_on_close` — stop the loop when the window closes
After `self._stop()`:
```python
        self.loop.call_soon_threadsafe(self.loop.stop)
```

## How to verify
1. `python -m py_compile gui.py` → no error.
2. Run the app, link to MT5, click **Start trading**:
   - It should flip to `● Running — trading the signals` within a second or two.
   - If MT5 is not actually logged in, you now get a clear **"Could not start"**
     dialog (within ~60s) instead of a silent hang.
3. Click **Stop trading** → returns to `Stopped`, no error.
4. Activity log should show `Signal watcher started — polling every …s`.

## Notes
- If it still hangs *before* "Running" even after this, the block is inside
  `connect_mt5()` → `mt5.initialize()` (MT5 busy/launching). The 60s timeout now
  turns that into an error dialog rather than an infinite "Starting…".
- No new dependency. Minimal change — only `gui.py` is touched.
