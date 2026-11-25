"""
Beyond Signal Bot — desktop app (customer build, Windows).

A clean window instead of a console: the customer connects their Beyond Signal account, types their
MetaTrader 5 login, ticks the pairs to trade and clicks Start. No files to edit, no terminal, no code.

This is the PyInstaller entry point for customer builds (see customer/BUILD_WINDOWS.md). The server-side
bot still runs through main.py. Trading rules, lot limits and the signal feed all come from the Beyond
Signal server; the customer only chooses account, lot and pairs.

UI: CustomTkinter (modern, rounded widgets). Widgets are touched only on the main thread. Background work
(linking, connecting, the trading loop) runs in worker threads and marshals UI updates back with
root.after(0, ...). Every log line from the trading modules is captured by a logging handler and shown
in the activity panel.
"""
from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

import pair_icons

from config import get_settings
from portal_link import (
    CLIENT_VERSION,
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
from settings_store import clamp_lot, load_settings, pairs_to_trade, save_settings

SERVER_URL = os.environ.get("SIGNALTRADE_URL", "https://signal.beyond-signal.net").rstrip("/")
LINK_TIMEOUT_S = 600

# Brand palette (light look)
TEAL = "#0f9488"
TEAL_HOVER = "#0b857a"
TEAL_BADGE = "#1fa99c"
TEAL_SOFT = "#e4f5f1"
TEAL_INK = "#0b5a53"
GREEN = "#0e9f6e"
AMBER = "#b45309"
MUTED = "#5c6c73"
CARD = "#ffffff"
CARD_BORDER = "#e6ecee"
PAGE = "#eef2f3"


class BotApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.log_q: queue.Queue[str] = queue.Queue()
        self.running = False
        self.linking = False
        self.device_key = load_device_key()
        self.allowed_pairs: list[str] = []
        self.pair_vars: dict[str, tk.BooleanVar] = {}
        self.pair_widgets: list[ctk.CTkCheckBox] = []
        self._pair_imgs: list[ctk.CTkImage] = []
        self.entries: list[ctk.CTkEntry] = []
        self._link = None  # (code, poll_secret) while a link is in progress

        root.title("Beyond Signal Bot")
        root.geometry("470x790")
        root.minsize(430, 620)
        root.configure(fg_color=PAGE)

        self._build()
        self._attach_logging()
        self._load_saved()
        self._refresh_link_state()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(300, self._drain_log)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        # Header (always on top)
        head = ctk.CTkFrame(self.root, corner_radius=0, fg_color=TEAL, height=74)
        head.pack(side="top", fill="x")
        hin = ctk.CTkFrame(head, fg_color="transparent")
        hin.pack(fill="x", padx=18, pady=14)
        ctk.CTkLabel(hin, text="🤖", width=38, height=38, corner_radius=11, fg_color=TEAL_BADGE,
                     text_color="white", font=ctk.CTkFont(size=18)).pack(side="left")
        tf = ctk.CTkFrame(hin, fg_color="transparent")
        tf.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(tf, text="Beyond Signal Bot", text_color="white",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(tf, text="Trading your signals, hands-off", text_color="#d9f3ee",
                     font=ctk.CTkFont(size=11)).pack(anchor="w")
        ctk.CTkLabel(hin, text=f"v{CLIENT_VERSION}", text_color="white", fg_color=TEAL_BADGE, corner_radius=8,
                     width=52, height=24, font=ctk.CTkFont(size=12, weight="bold")).pack(side="right")

        # Bottom action bar: Start/Stop + status (pinned — never scrolls off)
        bar = ctk.CTkFrame(self.root, corner_radius=0, fg_color=CARD, border_width=1, border_color=CARD_BORDER)
        bar.pack(side="bottom", fill="x")
        barin = ctk.CTkFrame(bar, fg_color="transparent")
        barin.pack(fill="x", padx=18, pady=(14, 16))
        self.start_btn = ctk.CTkButton(barin, text="▶  Start trading", command=self._on_start,
                                       height=46, corner_radius=12, fg_color=TEAL, hover_color=TEAL_HOVER,
                                       text_color="white", font=ctk.CTkFont(size=15, weight="bold"))
        self.start_btn.pack(fill="x")
        self.status_label = ctk.CTkLabel(barin, text="Stopped", text_color=MUTED,
                                         font=ctk.CTkFont(size=12, weight="bold"), anchor="w")
        self.status_label.pack(anchor="w", pady=(8, 0))

        # Activity log (fixed height, above the bar — stays visible)
        logwrap = ctk.CTkFrame(self.root, fg_color="transparent")
        logwrap.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        self.log = ctk.CTkTextbox(logwrap, height=104, corner_radius=11, fg_color="#0a1416",
                                  text_color="#8fe9db", border_width=1, border_color=CARD_BORDER,
                                  font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

        # Scrollable middle (sections 1–3)
        body = ctk.CTkScrollableFrame(self.root, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True, padx=2, pady=(10, 0))

        # 1 · account link
        b1 = self._card(body, "1", "Beyond Signal account")
        self.link_status = ctk.CTkLabel(b1, text="", wraplength=360, justify="left", anchor="w",
                                        font=ctk.CTkFont(size=13))
        self.link_status.pack(fill="x")
        self.code_label = ctk.CTkLabel(b1, text="", text_color=TEAL,
                                       font=ctk.CTkFont(family="Consolas", size=22, weight="bold"))
        self.link_btn = ctk.CTkButton(b1, text="Connect account", command=self._on_connect,
                                      height=38, corner_radius=10, fg_color=TEAL, hover_color=TEAL_HOVER,
                                      text_color="white", font=ctk.CTkFont(size=13, weight="bold"))
        self.link_btn.pack(anchor="w", pady=(10, 0))

        # 2 · trade settings (the bot uses whatever account you're logged into in MT5 — no login needed)
        b2 = self._card(body, "2", "Trade settings")
        self.var_lot = tk.StringVar(value="0.01")
        ctk.CTkLabel(b2, text="The bot trades the account you're signed into in MetaTrader 5. "
                             "Just open MT5 and log in — no account number or password needed here.",
                     wraplength=360, justify="left", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(fill="x", pady=(0, 4))
        self._field(b2, "Lot size per trade (0.01–1.00)", self.var_lot, full=False)

        # 3 · pairs
        b3 = self._card(body, "3", "Pairs to trade")
        ptop = ctk.CTkFrame(b3, fg_color="transparent")
        ptop.pack(fill="x")
        self.pairs_hint = ctk.CTkLabel(ptop, text="Connect your account to see the pairs your plan allows.",
                                       wraplength=240, justify="left", anchor="w", text_color=MUTED,
                                       font=ctk.CTkFont(size=12))
        self.pairs_hint.pack(side="left", fill="x", expand=True)
        self.sel_all_btn = ctk.CTkButton(ptop, text="Select all", width=80, height=30, corner_radius=9,
                                         fg_color=TEAL, hover_color=TEAL_HOVER, text_color="white",
                                         font=ctk.CTkFont(size=12, weight="bold"), command=self._select_all)
        self.clr_all_btn = ctk.CTkButton(ptop, text="Clear all", width=78, height=30, corner_radius=9,
                                         fg_color="transparent", hover_color=TEAL_SOFT, text_color=TEAL_INK,
                                         border_width=1, border_color=TEAL, font=ctk.CTkFont(size=12, weight="bold"),
                                         command=self._clear_all)
        self.pairs_grid = ctk.CTkFrame(b3, fg_color="transparent")
        self.pairs_grid.pack(fill="x", pady=(10, 0))

    def _card(self, parent, n: str, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD, border_width=1, border_color=CARD_BORDER)
        card.pack(fill="x", padx=12, pady=(0, 14))
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(top, text=n, width=24, height=24, corner_radius=7, fg_color=TEAL_SOFT,
                     text_color=TEAL_INK, font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(top, text=title, font=ctk.CTkFont(size=14, weight="bold")).pack(side="left", padx=(9, 0))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(0, 16))
        return inner

    def _field(self, parent, label, var, show=None, full=True) -> None:
        ctk.CTkLabel(parent, text=label, text_color=MUTED, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(fill="x", pady=(8, 2))
        e = ctk.CTkEntry(parent, textvariable=var, show=show, height=38, corner_radius=10)
        if full:
            e.pack(fill="x")
        else:
            e.configure(width=150)
            e.pack(anchor="w")
        self.entries.append(e)

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
        if s.get("lot_size"):
            self.var_lot.set(str(s["lot_size"]))
        self._saved_pairs = list(s.get("pairs") or [])

    def _collect(self) -> dict:
        return {
            "lot_size": clamp_lot(self.var_lot.get()),
            "pairs": [p for p, v in self.pair_vars.items() if v.get()],
        }

    # ── link state ─────────────────────────────────────────────────────────────
    def _refresh_link_state(self) -> None:
        if self.device_key:
            self.link_status.configure(text="✓ Linked to your Beyond Signal account.", text_color=GREEN)
            self.code_label.pack_forget()
            self.link_btn.configure(text="Unlink", command=self._on_unlink, fg_color="transparent",
                                    text_color=MUTED, border_width=1, border_color=CARD_BORDER, hover_color=PAGE)
            self._load_pairs_async()
        else:
            self.link_status.configure(
                text="Connect this bot to your account. You'll get a code to enter at "
                     f"{SERVER_URL}/bot while signed in (Premium).", text_color=MUTED)
            self.link_btn.configure(text="Connect account", command=self._on_connect, state="normal",
                                    fg_color=TEAL, text_color="white", border_width=0, hover_color=TEAL_HOVER)
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        ok = bool(self.device_key) and not self.running
        self.start_btn.configure(state="normal" if ok else "disabled")

    def _on_connect(self) -> None:
        if self.linking:
            return
        self.linking = True
        self.link_btn.configure(state="disabled")
        self.link_status.configure(text="Starting…", text_color=MUTED)

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
        self.link_status.configure(
            text=f"Enter this code at {SERVER_URL}/bot (signed in, Premium). Waiting for approval…", text_color=MUTED)
        self.code_label.configure(text=code)
        self.code_label.pack(anchor="w", pady=(8, 0))

    def _linked_ok(self) -> None:
        self.linking = False
        self._log("Bot linked to your account.")
        self._refresh_link_state()

    def _link_failed(self, msg: str) -> None:
        self.linking = False
        self.link_status.configure(text=msg, text_color=MUTED)
        self.link_btn.configure(state="normal")

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
        self.pair_widgets = []
        self.sel_all_btn.pack_forget()
        self.clr_all_btn.pack_forget()
        self.pairs_hint.configure(text="Connect your account to see the pairs your plan allows.")
        self._refresh_link_state()

    def _select_all(self) -> None:
        for v in self.pair_vars.values():
            v.set(True)

    def _clear_all(self) -> None:
        for v in self.pair_vars.values():
            v.set(False)

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
        self.pair_widgets = []
        if not pairs:
            self.sel_all_btn.pack_forget()
            self.clr_all_btn.pack_forget()
            self.pairs_hint.configure(text="No pairs available yet — check your plan includes the Trading Bot.")
            return
        self.pairs_hint.configure(text="Your plan allows these. Tick the ones to trade.")
        self.sel_all_btn.pack(side="right", padx=(6, 0))
        self.clr_all_btn.pack(side="right")
        chosen = set(self._saved_pairs) if getattr(self, "_saved_pairs", None) else set(pairs)
        self._pair_imgs = []
        self.pairs_grid.columnconfigure((0, 1), weight=1)
        for i, p in enumerate(pairs):
            var = tk.BooleanVar(value=p in chosen)
            self.pair_vars[p] = var
            cell = ctk.CTkFrame(self.pairs_grid, fg_color="transparent")
            cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=4, pady=3)
            cb = ctk.CTkCheckBox(cell, text="", variable=var, onvalue=True, offvalue=False, width=22,
                                 checkbox_width=18, checkbox_height=18, corner_radius=5,
                                 fg_color=TEAL, hover_color=TEAL_HOVER)
            cb.pack(side="left")
            self._pair_icon(cell, p).pack(side="left", padx=(2, 8))
            name = pair_icons.display_name(p)
            txt = ctk.CTkFrame(cell, fg_color="transparent")
            txt.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(txt, text=name, font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(anchor="w")
            if name != p:
                ctk.CTkLabel(txt, text=p, font=ctk.CTkFont(size=10), text_color=MUTED, anchor="w").pack(anchor="w")
            self.pair_widgets.append(cb)

    def _pair_icon(self, parent, symbol: str) -> ctk.CTkLabel:
        path = pair_icons.logo_path(symbol)
        if path:
            try:
                img = ctk.CTkImage(Image.open(path), size=(24, 24))
                self._pair_imgs.append(img)
                return ctk.CTkLabel(parent, text="", image=img, width=24)
            except Exception:
                pass
        return ctk.CTkLabel(parent, text=symbol[:2].upper(), width=24, height=24, corner_radius=6,
                            fg_color=TEAL_SOFT, text_color=TEAL_INK, font=ctk.CTkFont(size=10, weight="bold"))

    # ── start / stop ───────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        data = self._collect()
        if not self.device_key:
            messagebox.showerror("Connect first", "Connect your Beyond Signal account first.")
            return
        save_settings(data)

        self.start_btn.configure(state="disabled")
        self.status_label.configure(text="Starting…", text_color=AMBER)
        threading.Thread(target=self._start_worker, args=(data,), daemon=True).start()

    def _start_worker(self, data: dict) -> None:
        try:
            s = get_settings()
            s.portal_mode = True
            s.signaltrade_url = SERVER_URL
            # No MT5 credentials: the bot attaches to the terminal the customer already has open (see mt5_service.connect_mt5)
            s.mt5_login = 0
            s.mt5_password = ""
            s.mt5_server = ""
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
        self.status_label.configure(text="● Running — trading the signals", text_color=GREEN)
        self.start_btn.configure(text="■  Stop trading", command=self._on_stop, state="normal",
                                 fg_color="#b91c1c", hover_color="#991b1b")
        self._set_inputs_enabled(False)

    def _start_failed(self, msg: str) -> None:
        self.status_label.configure(text="Stopped", text_color=MUTED)
        self.start_btn.configure(state="normal")
        messagebox.showerror("Could not start", msg)

    def _on_stop(self) -> None:
        self._stop()
        self.status_label.configure(text="Stopped", text_color=MUTED)
        self.start_btn.configure(text="▶  Start trading", command=self._on_start,
                                 fg_color=TEAL, hover_color=TEAL_HOVER)
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
        for w in (*self.entries, *self.pair_widgets, self.link_btn, self.sel_all_btn, self.clr_all_btn):
            try:
                w.configure(state=state)
            except Exception:
                pass

    def _on_close(self) -> None:
        # Closing the window stops the bot (the customer's choice): no hidden background trading.
        if self.running and not messagebox.askokcancel("Quit", "This will stop the bot and close any monitoring. Quit?"):
            return
        self._stop()
        self.root.destroy()


def run_app() -> None:
    ctk.set_appearance_mode("light")  # clean light look; matches the portal
    root = ctk.CTk()
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_app()
