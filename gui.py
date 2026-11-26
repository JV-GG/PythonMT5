"""
Beyond Signal Bot — desktop app (customer build, Windows).

A clean window instead of a console: the customer connects their Beyond Signal account, picks the pairs
to trade and clicks Start. No files to edit, no terminal, no code.

This is the PyInstaller entry point for customer builds (see customer/BUILD_WINDOWS.md). The server-side
bot still runs through main.py. Trading rules, lot limits and the signal feed all come from the Beyond
Signal server; the customer only chooses lot and pairs.

Layout: two columns — controls on the left, a live activity log on the right. Unlink lives on a Settings
page (gear in the header). CustomTkinter (modern, rounded widgets). Widgets are touched only on the main
thread. Background work (linking, connecting, the trading loop) runs in worker threads / the engine loop
and marshals UI updates back with root.after(0, ...). Every log line from the trading modules is captured
by a logging handler and shown in the activity panel.
"""
from __future__ import annotations

import asyncio
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

# ── Refined light palette (colour goes on data/action, not chrome) ───────────────
GROUND = "#f4f6f6"       # window background
SURFACE = "#ffffff"      # cards
EDGE = "#e7eaec"         # card border
RULE = "#eef1f2"         # inner divider
INK = "#16221f"          # primary text
MUTED = "#5c6b70"        # secondary text
FAINT = "#6b797e"        # tertiary text

TEAL = "#0e857a"
TEAL_HOVER = "#0b6b62"
TEAL_INK = "#0b6b62"
TEAL_TINT = "#eaf5f2"
TEAL_EDGE = "#cfe6e0"

POS_BG = "#e4f4ea"       # connected pill
POS_INK = "#15603a"
WARN_BG = "#fdf4e2"      # not-connected pill / risk note
WARN_EDGE = "#f0dcae"
WARN_INK = "#8a5a12"
NEUTRAL_BG = "#eef1f2"   # version pill
NEUTRAL_INK = "#46555a"
NEG_EDGE = "#eecbc6"     # unlink button
NEG_INK = "#a93423"
GREEN = "#15834f"
STOP_RED = "#b91c1c"
STOP_RED_HOVER = "#991b1b"

TERM = "#0e1113"         # activity log panel
TERM_EDGE = "#0a0d0e"
TERM_INK = "#aeb9bc"


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

        # The trading engine (signal watcher + trade monitor) is asyncio-based and needs a LIVE
        # event loop. A Tk GUI has none, so we run one in a background thread for the engine.
        # Without it, start_watcher()/start_monitor() raise "no running event loop" and
        # "Start trading" hangs forever on "Starting…".
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()

        root.title("Beyond Signal Bot")
        root.geometry("940x628")
        root.minsize(880, 560)
        root.configure(fg_color=GROUND)

        self._build()
        self._attach_logging()
        self._load_saved()
        self._refresh_link_state()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(300, self._drain_log)

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self._build_header()

        # One container holds either the main two-column view or the settings page.
        self.main_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main_frame.pack(side="top", fill="both", expand=True)
        self._build_main(self.main_frame)

        self.settings_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        self._build_settings(self.settings_frame)

    def _build_header(self) -> None:
        head = ctk.CTkFrame(self.root, corner_radius=0, fg_color=SURFACE, height=68,
                            border_width=0)
        head.pack(side="top", fill="x")
        hin = ctk.CTkFrame(head, fg_color="transparent")
        hin.pack(fill="x", padx=18, pady=12)
        ctk.CTkLabel(hin, text="🤖", width=40, height=40, corner_radius=11, fg_color=TEAL,
                     text_color="white", font=ctk.CTkFont(size=19)).pack(side="left")
        tf = ctk.CTkFrame(hin, fg_color="transparent")
        tf.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(tf, text="Beyond Signal Bot", text_color=INK,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(tf, text="Trading your signals, hands-off", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w")

        # right side: settings gear · version pill · status pill
        self.gear_btn = ctk.CTkButton(hin, text="⚙", width=36, height=32, corner_radius=9,
                                      fg_color="transparent", hover_color=NEUTRAL_BG, text_color=MUTED,
                                      border_width=1, border_color=EDGE,
                                      font=ctk.CTkFont(size=16), command=self._show_settings)
        self.gear_btn.pack(side="right", padx=(10, 0))
        ctk.CTkLabel(hin, text=f"v{CLIENT_VERSION}", text_color=NEUTRAL_INK, fg_color=NEUTRAL_BG,
                     corner_radius=8, width=50, height=24,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(side="right", padx=(10, 0))
        self.conn_pill = ctk.CTkLabel(hin, text="Not connected", text_color=WARN_INK, fg_color=WARN_BG,
                                      corner_radius=8, height=26, font=ctk.CTkFont(size=12, weight="bold"))
        self.conn_pill.pack(side="right", padx=(10, 0), ipadx=9)

    def _set_conn_pill(self, state: str) -> None:
        if state == "running":
            self.conn_pill.configure(text="● Trading", text_color=POS_INK, fg_color=POS_BG)
        elif state == "linked":
            self.conn_pill.configure(text="● Connected", text_color=POS_INK, fg_color=POS_BG)
        else:
            self.conn_pill.configure(text="Not connected", text_color=WARN_INK, fg_color=WARN_BG)

    def _build_main(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0, minsize=306)
        parent.rowconfigure(0, weight=1)

        # LEFT: scrollable cards + pinned Start button
        left = ctk.CTkFrame(parent, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(14, 7), pady=12)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        cards = ctk.CTkScrollableFrame(left, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="nsew")
        cards.columnconfigure(0, weight=1)

        # Connect / account card
        b1 = self._card(cards, "Beyond Signal account")
        self.link_status = ctk.CTkLabel(b1, text="", wraplength=440, justify="left", anchor="w",
                                        text_color=MUTED, font=ctk.CTkFont(size=13))
        self.link_status.pack(fill="x")
        self.code_row = ctk.CTkFrame(b1, fg_color=TEAL_TINT, corner_radius=12,
                                     border_width=1, border_color=TEAL_EDGE)
        self.code_label = ctk.CTkLabel(self.code_row, text="", text_color=TEAL_INK,
                                       font=ctk.CTkFont(family="Consolas", size=24, weight="bold"))
        self.code_label.pack(side="left", padx=(16, 10), pady=10)
        self.copy_btn = ctk.CTkButton(self.code_row, text="Copy", width=66, height=32, corner_radius=8,
                                      fg_color=SURFACE, hover_color=TEAL_TINT, text_color=TEAL_INK,
                                      border_width=1, border_color=TEAL_EDGE,
                                      font=ctk.CTkFont(size=12, weight="bold"), command=self._copy_code)
        self.copy_btn.pack(side="left", padx=(0, 12), pady=10)
        self.link_btn = ctk.CTkButton(b1, text="Connect account", command=self._on_connect,
                                      height=38, corner_radius=10, fg_color=TEAL, hover_color=TEAL_HOVER,
                                      text_color="white", font=ctk.CTkFont(size=13, weight="bold"))
        self.link_btn.pack(anchor="w", pady=(12, 0))

        # Trade settings card
        b2 = self._card(cards, "Trade settings")
        self.var_lot = tk.StringVar(value="0.01")
        ctk.CTkLabel(b2, text="The bot trades the account you're signed into in MetaTrader 5. "
                             "Open MT5 and log in — no account number or password needed here.",
                     wraplength=440, justify="left", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(fill="x", pady=(0, 4))
        self._field(b2, "Lot size per trade (0.01–1.00)", self.var_lot, full=False)
        note = ctk.CTkFrame(b2, fg_color=WARN_BG, corner_radius=9, border_width=1, border_color=WARN_EDGE)
        note.pack(fill="x", pady=(10, 0))
        ctk.CTkLabel(note, text="⚠  Keep risk small. With about $1,000 equity, stay at or below 0.05 lots "
                               "— larger sizes can lose fast.",
                     wraplength=430, justify="left", anchor="w", text_color=WARN_INK,
                     font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=11, pady=8)

        # Pairs card
        b3 = self._card(cards, "Pairs to trade")
        ptop = ctk.CTkFrame(b3, fg_color="transparent")
        ptop.pack(fill="x")
        self.pairs_hint = ctk.CTkLabel(ptop, text="Connect your account to see the pairs your plan allows.",
                                       wraplength=300, justify="left", anchor="w", text_color=MUTED,
                                       font=ctk.CTkFont(size=12))
        self.pairs_hint.pack(side="left", fill="x", expand=True)
        self.sel_all_btn = ctk.CTkButton(ptop, text="Select all", width=80, height=30, corner_radius=9,
                                         fg_color=TEAL, hover_color=TEAL_HOVER, text_color="white",
                                         font=ctk.CTkFont(size=12, weight="bold"), command=self._select_all)
        self.clr_all_btn = ctk.CTkButton(ptop, text="Clear all", width=78, height=30, corner_radius=9,
                                         fg_color="transparent", hover_color=TEAL_TINT, text_color=TEAL_INK,
                                         border_width=1, border_color=TEAL, font=ctk.CTkFont(size=12, weight="bold"),
                                         command=self._clear_all)
        self.pairs_grid = ctk.CTkFrame(b3, fg_color="transparent")
        self.pairs_grid.pack(fill="x", pady=(10, 0))

        # Pinned Start/Stop + status
        startwrap = ctk.CTkFrame(left, fg_color="transparent")
        startwrap.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.start_btn = ctk.CTkButton(startwrap, text="▶  Start trading", command=self._on_start,
                                       height=48, corner_radius=13, fg_color=TEAL, hover_color=TEAL_HOVER,
                                       text_color="white", font=ctk.CTkFont(size=15, weight="bold"))
        self.start_btn.pack(fill="x")
        self.status_label = ctk.CTkLabel(startwrap, text="Stopped", text_color=MUTED,
                                         font=ctk.CTkFont(size=12, weight="bold"), anchor="w")
        self.status_label.pack(anchor="w", pady=(7, 0))

        # RIGHT: activity log (full height)
        right = ctk.CTkFrame(parent, fg_color=TERM, corner_radius=14, border_width=1, border_color=TERM_EDGE)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=12)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        lh = ctk.CTkFrame(right, fg_color="transparent")
        lh.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 8))
        ctk.CTkLabel(lh, text="❯  Activity log", text_color="#d6e0e2",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        ctk.CTkLabel(lh, text="live", text_color="#5c6c70", font=ctk.CTkFont(size=10)).pack(side="right")
        self.log = ctk.CTkTextbox(right, corner_radius=0, fg_color=TERM, text_color=TERM_INK,
                                  border_width=0, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")
        self.log.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.log.configure(state="disabled")

    def _build_settings(self, parent) -> None:
        # header
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkButton(top, text="←  Back", width=78, height=32, corner_radius=9, fg_color="transparent",
                      hover_color=NEUTRAL_BG, text_color=MUTED, border_width=1, border_color=EDGE,
                      font=ctk.CTkFont(size=13, weight="bold"), command=self._show_main).pack(side="left")
        ctk.CTkLabel(top, text="Settings", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=INK).pack(side="left", padx=(12, 0))

        wrap = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        wrap.columnconfigure(0, weight=1)

        # Account
        acc = self._card(wrap, "Account")
        self.settings_account = ctk.CTkLabel(acc, text="", wraplength=600, justify="left", anchor="w",
                                             text_color=MUTED, font=ctk.CTkFont(size=13))
        self.settings_account.pack(fill="x")
        row = ctk.CTkFrame(acc, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text="Unlink this computer", anchor="w", text_color=INK,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(txt, text="Stops the bot here. You can link again any time with a new code.",
                     anchor="w", text_color=FAINT, wraplength=520, justify="left",
                     font=ctk.CTkFont(size=11)).pack(anchor="w")
        self.unlink_btn = ctk.CTkButton(row, text="Unlink", width=96, height=36, corner_radius=9,
                                        fg_color="transparent", hover_color="#fbeae7", text_color=NEG_INK,
                                        border_width=1, border_color=NEG_EDGE,
                                        font=ctk.CTkFont(size=12, weight="bold"), command=self._on_unlink)
        self.unlink_btn.pack(side="right")

        # About
        ab = self._card(wrap, "About")
        ctk.CTkLabel(ab, text=f"Beyond Signal Bot · v{CLIENT_VERSION}", anchor="w", text_color=INK,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(ab, text="You're on the latest version.", anchor="w", text_color=FAINT,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(2, 0))

    def _show_settings(self) -> None:
        self._refresh_settings_account()
        self.main_frame.pack_forget()
        self.settings_frame.pack(side="top", fill="both", expand=True)

    def _show_main(self) -> None:
        self.settings_frame.pack_forget()
        self.main_frame.pack(side="top", fill="both", expand=True)

    def _refresh_settings_account(self) -> None:
        if self.device_key:
            self.settings_account.configure(text="✓ Linked to your Beyond Signal account.", text_color=GREEN)
            self.unlink_btn.configure(state="normal")
        else:
            self.settings_account.configure(text="Not linked yet. Connect your account from the main screen.",
                                            text_color=MUTED)
            self.unlink_btn.configure(state="disabled")

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=14, fg_color=SURFACE, border_width=1, border_color=EDGE)
        card.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(card, text=title, text_color=INK,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=16, pady=(14, 2))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=(4, 16))
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

    def _copy_code(self) -> None:
        code = self.code_label.cget("text")
        if not code:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            self.copy_btn.configure(text="Copied")
            self.root.after(1200, lambda: self.copy_btn.configure(text="Copy"))
        except Exception:
            pass

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
        if self.running:
            self._refresh_running_status()
        self.root.after(300, self._drain_log)

    def _refresh_running_status(self) -> None:
        """While running, say honestly whether signals are arriving (not just "Running" during a server outage)."""
        import signal_watcher
        down = signal_watcher.feed_down_minutes()
        if signal_watcher.portal_block_new:
            text, color = "● Plan ended — managing open trades, no new ones", WARN_INK
        elif down is not None and down >= 1:
            text, color = f"● Running — waiting for signals (server not responding for {down:.0f} min)", WARN_INK
        else:
            text, color = "● Running — trading the signals", GREEN
        if self.status_label.cget("text") != text:
            self.status_label.configure(text=text, text_color=color)

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
            self.link_status.configure(text="✓ Linked to your Beyond Signal account. "
                                            "Manage or unlink in Settings (⚙).", text_color=GREEN)
            self.code_row.pack_forget()
            self.link_btn.pack_forget()
            self._set_conn_pill("linked")
            self._load_pairs_async()
        else:
            self.link_status.configure(
                text="Connect this bot to your account. You'll get a code to enter at "
                     f"{SERVER_URL}/bot while signed in (Premium).", text_color=MUTED)
            self.link_btn.configure(text="Connect account", command=self._on_connect, state="normal",
                                    fg_color=TEAL, text_color="white", border_width=0, hover_color=TEAL_HOVER)
            self.link_btn.pack(anchor="w", pady=(12, 0))
            self._set_conn_pill("off")
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
        self.code_row.pack(anchor="w", pady=(10, 0))

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
        self._refresh_settings_account()
        self._refresh_link_state()
        self._show_main()

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
            ctk.CTkLabel(txt, text=name, font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
                         text_color=INK).pack(anchor="w")
            if name != p:
                ctk.CTkLabel(txt, text=p, font=ctk.CTkFont(size=10), text_color=FAINT, anchor="w").pack(anchor="w")
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
                            fg_color=NEUTRAL_BG, text_color=NEUTRAL_INK, font=ctk.CTkFont(size=10, weight="bold"))

    # ── start / stop ───────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        data = self._collect()
        if not self.device_key:
            messagebox.showerror("Connect first", "Connect your Beyond Signal account first.")
            return
        save_settings(data)

        self.start_btn.configure(state="disabled")
        self.status_label.configure(text="Starting…", text_color=WARN_INK)
        threading.Thread(target=self._start_worker, args=(data,), daemon=True).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

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
        except (PlanEnded, Unlinked) as e:
            self.root.after(0, lambda: self._start_failed(str(e)))
        except Exception as e:
            self.root.after(0, lambda: self._start_failed(str(e)))

    def _started_ok(self) -> None:
        self.running = True
        self.status_label.configure(text="● Running — trading the signals", text_color=GREEN)
        self.start_btn.configure(text="■  Stop trading", command=self._on_stop, state="normal",
                                 fg_color=STOP_RED, hover_color=STOP_RED_HOVER)
        self._set_conn_pill("running")
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
        self._set_conn_pill("linked" if self.device_key else "off")
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

            # Cancel the tasks and disconnect on the engine loop (same thread they run on).
            async def _end():
                stop_watcher()
                stop_monitor()
                disconnect_mt5()

            asyncio.run_coroutine_threadsafe(_end(), self.loop).result(timeout=15)
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
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.root.destroy()


def run_app() -> None:
    ctk.set_appearance_mode("light")  # clean light look; matches the portal
    root = ctk.CTk()
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_app()
