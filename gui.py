"""
Beyond Signal Bot — desktop app (customer build, Windows).

A plain window instead of a console: the customer connects their Beyond Signal account, types their
MetaTrader 5 login, ticks the pairs to trade and clicks Start. No files to edit, no terminal, no code.

This is the PyInstaller entry point for customer builds (see customer/BUILD_WINDOWS.md). The server-side
bot still runs through main.py. Trading rules, lot limits and the signal feed all come from the Beyond
Signal server; the customer only chooses account, lot and pairs.

Threading: Tkinter widgets are touched only on the main thread. Background work (linking, connecting,
the trading loop) runs in worker threads and marshals UI updates back with root.after(0, ...). Every
log line from the trading modules is captured by a logging handler and shown in the activity panel.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from config import get_settings
from portal_link import (
    PlanEnded,
    Unlinked,
    clear_device_key,
    fetch_config,
    fetch_plan_pairs,
    link_poll,
    link_start,
    load_device_key,
    version_ok,
)
from settings_store import clamp_lot, load_settings, pairs_to_trade, save_settings, valid_account

SERVER_URL = os.environ.get("SIGNALTRADE_URL", "https://signal.beyond-signal.net").rstrip("/")
LINK_TIMEOUT_S = 600


class BotApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.log_q: queue.Queue[str] = queue.Queue()
        self.running = False
        self.linking = False
        self.device_key = load_device_key()
        self.allowed_pairs: list[str] = []
        self.pair_vars: dict[str, tk.BooleanVar] = {}
        self._link = None  # (code, poll_secret) while a link is in progress

        root.title("Beyond Signal Bot")
        root.geometry("460x720")
        root.minsize(420, 600)

        self._build()
        self._attach_logging()
        self._load_saved()
        self._refresh_link_state()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(300, self._drain_log)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        pad = {"padx": 12, "pady": (0, 10)}
        head = ttk.Frame(self.root)
        head.pack(fill="x", padx=12, pady=12)
        ttk.Label(head, text="Beyond Signal Bot", font=("Segoe UI", 14, "bold")).pack(side="left")
        ttk.Label(head, text="v1.0.0", foreground="#888").pack(side="right")

        # 1 · account link
        self.link_box = ttk.LabelFrame(self.root, text="1 · Beyond Signal account")
        self.link_box.pack(fill="x", **pad)
        self.link_status = ttk.Label(self.link_box, text="", wraplength=400, justify="left")
        self.link_status.pack(fill="x", padx=10, pady=(8, 6))
        self.code_label = ttk.Label(self.link_box, text="", font=("Consolas", 20, "bold"), foreground="#0f766e")
        self.link_btn = ttk.Button(self.link_box, text="Connect account", command=self._on_connect)
        self.link_btn.pack(padx=10, pady=(0, 10))

        # 2 · MT5 login
        self.mt5_box = ttk.LabelFrame(self.root, text="2 · MetaTrader 5 login")
        self.mt5_box.pack(fill="x", **pad)
        self.var_login = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_server = tk.StringVar()
        self.var_lot = tk.StringVar(value="0.01")
        self._field(self.mt5_box, "Account number", self.var_login)
        self._field(self.mt5_box, "Password", self.var_pass, show="•")
        self._field(self.mt5_box, "Server", self.var_server)
        self._field(self.mt5_box, "Lot size per trade (0.01–1.00)", self.var_lot, width=10)

        # 3 · pairs
        self.pairs_box = ttk.LabelFrame(self.root, text="3 · Pairs to trade")
        self.pairs_box.pack(fill="x", **pad)
        self.pairs_hint = ttk.Label(self.pairs_box, text="Connect your account to see the pairs your plan allows.",
                                    wraplength=400, foreground="#888")
        self.pairs_hint.pack(fill="x", padx=10, pady=8)
        self.pairs_grid = ttk.Frame(self.pairs_box)
        self.pairs_grid.pack(fill="x", padx=10, pady=(0, 8))

        # controls + status
        self.start_btn = ttk.Button(self.root, text="▶  Start trading", command=self._on_start)
        self.start_btn.pack(fill="x", padx=12, pady=(0, 8))
        self.status_label = ttk.Label(self.root, text="Stopped", foreground="#888")
        self.status_label.pack(anchor="w", padx=14)
        self.log = scrolledtext.ScrolledText(self.root, height=9, state="disabled",
                                             font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=10)

    def _field(self, parent, label, var, show=None, width=28) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text=label).pack(anchor="w")
        ttk.Entry(row, textvariable=var, show=show, width=width).pack(anchor="w", fill="x")

    # ── logging into the panel ─────────────────────────────────────────────────
    def _attach_logging(self) -> None:
        q = self.log_q

        class QueueHandler(logging.Handler):
            def emit(self, record):
                try:
                    q.put_nowait(self.format(record))
                except Exception:
                    pass

        h = QueueHandler()
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(h)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def _log(self, line: str) -> None:
        self.log_q.put_nowait(line)

    def _drain_log(self) -> None:
        lines = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log.configure(state="normal")
            for ln in lines:
                self.log.insert("end", ln + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(300, self._drain_log)

    # ── saved settings ─────────────────────────────────────────────────────────
    def _load_saved(self) -> None:
        s = load_settings()
        if s.get("mt5_login"):
            self.var_login.set(str(s["mt5_login"]))
        if s.get("mt5_password"):
            self.var_pass.set(str(s["mt5_password"]))
        if s.get("mt5_server"):
            self.var_server.set(str(s["mt5_server"]))
        if s.get("lot_size"):
            self.var_lot.set(str(s["lot_size"]))
        self._saved_pairs = list(s.get("pairs") or [])

    def _collect(self) -> dict:
        return {
            "mt5_login": self.var_login.get().strip(),
            "mt5_password": self.var_pass.get(),
            "mt5_server": self.var_server.get().strip(),
            "lot_size": clamp_lot(self.var_lot.get()),
            "pairs": [p for p, v in self.pair_vars.items() if v.get()],
        }

    # ── link state ─────────────────────────────────────────────────────────────
    def _refresh_link_state(self) -> None:
        if self.device_key:
            self.link_status.config(text="✓ Linked to your Beyond Signal account.")
            self.code_label.pack_forget()
            self.link_btn.config(text="Unlink", command=self._on_unlink)
            self._load_pairs_async()
        else:
            self.link_status.config(text="Connect this bot to your account. You'll get a code to enter at "
                                         f"{SERVER_URL}/bot while signed in (Premium).")
            self.link_btn.config(text="Connect account", command=self._on_connect, state="normal")
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        ok = bool(self.device_key) and not self.running
        self.start_btn.config(state="normal" if ok else "disabled")

    def _on_connect(self) -> None:
        if self.linking:
            return
        self.linking = True
        self.link_btn.config(state="disabled")
        self.link_status.config(text="Starting…")

        def work():
            try:
                code, secret = link_start(SERVER_URL, socket.gethostname())
                self.root.after(0, lambda: self._show_code(code))
                import time
                deadline = time.time() + LINK_TIMEOUT_S
                while time.time() < deadline and self.linking:
                    time.sleep(3)
                    key = link_poll(SERVER_URL, code, secret)
                    if key:
                        self.device_key = key
                        self.root.after(0, self._linked_ok)
                        return
                self.root.after(0, lambda: self._link_failed("Link not approved in time. Try again."))
            except Exception as e:
                self.root.after(0, lambda: self._link_failed(f"Could not connect: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_code(self, code: str) -> None:
        self.link_status.config(text=f"Enter this code at {SERVER_URL}/bot (signed in, Premium). Waiting for approval…")
        self.code_label.config(text=code)
        self.code_label.pack(pady=(0, 8))

    def _linked_ok(self) -> None:
        self.linking = False
        self._log("Bot linked to your account.")
        self._refresh_link_state()

    def _link_failed(self, msg: str) -> None:
        self.linking = False
        self.link_status.config(text=msg)
        self.link_btn.config(state="normal")

    def _on_unlink(self) -> None:
        if self.running:
            messagebox.showinfo("Stop first", "Stop trading before unlinking.")
            return
        if not messagebox.askyesno("Unlink", "Unlink this bot from your account?"):
            return
        clear_device_key()
        self.device_key = None
        self.allowed_pairs = []
        for w in self.pairs_grid.winfo_children():
            w.destroy()
        self.pair_vars = {}
        self.pairs_hint.config(text="Connect your account to see the pairs your plan allows.")
        self._refresh_link_state()

    # ── pairs ──────────────────────────────────────────────────────────────────
    def _load_pairs_async(self) -> None:
        if self.allowed_pairs or not self.device_key:
            return

        def work():
            try:
                pairs = fetch_plan_pairs(SERVER_URL, self.device_key)
            except Exception:
                pairs = []
            self.root.after(0, lambda: self._render_pairs(pairs))

        threading.Thread(target=work, daemon=True).start()

    def _render_pairs(self, pairs: list[str]) -> None:
        self.allowed_pairs = pairs
        for w in self.pairs_grid.winfo_children():
            w.destroy()
        self.pair_vars = {}
        if not pairs:
            self.pairs_hint.config(text="No pairs available yet — check your plan includes the Trading Bot.")
            return
        self.pairs_hint.config(text="Your plan allows these. Tick the ones to trade.")
        chosen = set(self._saved_pairs) if getattr(self, "_saved_pairs", None) else set(pairs)
        for i, p in enumerate(pairs):
            var = tk.BooleanVar(value=p in chosen)
            self.pair_vars[p] = var
            ttk.Checkbutton(self.pairs_grid, text=p, variable=var).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=2)

    # ── start / stop ───────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        data = self._collect()
        if not valid_account(data["mt5_login"]):
            messagebox.showerror("Check login", "Enter your MT5 account number (digits only).")
            return
        if not data["mt5_password"] or not data["mt5_server"]:
            messagebox.showerror("Check login", "Enter your MT5 password and server.")
            return
        if not self.device_key:
            messagebox.showerror("Connect first", "Connect your Beyond Signal account first.")
            return
        save_settings(data)

        self.start_btn.config(state="disabled")
        self.status_label.config(text="Starting…", foreground="#b45309")
        threading.Thread(target=self._start_worker, args=(data,), daemon=True).start()

    def _start_worker(self, data: dict) -> None:
        try:
            s = get_settings()
            s.portal_mode = True
            s.signaltrade_url = SERVER_URL
            s.mt5_login = int(data["mt5_login"])
            s.mt5_password = data["mt5_password"]
            s.mt5_server = data["mt5_server"]
            s.lot_size = data["lot_size"]
            s.allowed_symbols = pairs_to_trade(data["pairs"], self.allowed_pairs) or list(self.allowed_pairs)

            cfg = fetch_config(SERVER_URL, self.device_key)
            if not version_ok(cfg.get("minClientVersion", "0")):
                raise RuntimeError("This bot is out of date. Download the latest version from the portal.")
            for k, v in (cfg.get("rules") or {}).items():
                if hasattr(s, k):
                    setattr(s, k, v)
            s.min_lot = float(cfg.get("minLot", s.min_lot))
            s.max_lot = float(cfg.get("maxLot", s.max_lot))
            s.lot_size = clamp_lot(s.lot_size, s.min_lot, s.max_lot)

            from mt5_service import connect_mt5, load_active_trades
            from signal_watcher import start_watcher
            from trade_monitor import start_monitor

            connect_mt5()
            load_active_trades()
            start_monitor()
            start_watcher()
            self.root.after(0, self._started_ok)
        except (PlanEnded, Unlinked) as e:
            self.root.after(0, lambda: self._start_failed(str(e)))
        except Exception as e:
            self.root.after(0, lambda: self._start_failed(str(e)))

    def _started_ok(self) -> None:
        self.running = True
        self.status_label.config(text="● Running — trading the signals", foreground="#047857")
        self.start_btn.config(text="■  Stop trading", command=self._on_stop, state="normal")
        self._set_inputs_enabled(False)

    def _start_failed(self, msg: str) -> None:
        self.status_label.config(text="Stopped", foreground="#888")
        self.start_btn.config(state="normal")
        messagebox.showerror("Could not start", msg)

    def _on_stop(self) -> None:
        self._stop()
        self.status_label.config(text="Stopped", foreground="#888")
        self.start_btn.config(text="▶  Start trading", command=self._on_start)
        self._set_inputs_enabled(True)
        self._update_start_enabled()

    def _stop(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            from signal_watcher import stop_watcher
            from trade_monitor import stop_monitor
            from mt5_service import disconnect_mt5
            stop_watcher()
            stop_monitor()
            disconnect_mt5()
        except Exception as e:
            self._log(f"Error while stopping: {e}")

    def _set_inputs_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        for box in (self.mt5_box, self.pairs_grid):
            for w in box.winfo_children():
                for leaf in (w.winfo_children() or [w]):
                    try:
                        leaf.config(state=state)
                    except tk.TclError:
                        pass
        try:
            self.link_btn.config(state=state)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        # Closing the window stops the bot (the customer's choice): no hidden background trading.
        if self.running and not messagebox.askokcancel("Quit", "This will stop the bot and close any monitoring. Quit?"):
            return
        self._stop()
        self.root.destroy()


def run_app() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")  # native-looking on Windows; ignored elsewhere
    except tk.TclError:
        pass
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_app()
