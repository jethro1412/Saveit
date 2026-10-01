import asyncio
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

import customtkinter as ctk
from dotenv import get_key, load_dotenv, set_key

from engine import (
    RateLimiter,
    SaveitEngine,
    cleanup_empty_downloads_dir,
    parse_group_targets,
)
from tracker import FileTracker

# Set appearance mode and color theme
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

ENV_PATH = Path(".env")
ENV_EXAMPLE_PATH = Path(".env.example")


class AuthDialog(ctk.CTkToplevel):
    """
    Modal dialog for Telegram Interactive Authentication.
    Guides the user through: Phone Number -> Login Code -> 2FA Password.
    """

    def __init__(self, parent, mode="phone", phone="", error_message=""):
        super().__init__(parent)
        self.parent = parent
        self.mode = mode
        self.phone = phone
        self.result = None

        self.title("Telegram Authentication")
        self.geometry("450x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Center on parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_x()
        py = parent.winfo_y()
        cx = px + (pw - 450) // 2
        cy = py + (ph - 300) // 2
        self.geometry(f"+{max(0, cx)}+{max(0, cy)}")

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Header
        self.header_label = ctk.CTkLabel(
            self,
            text="🔐 Telegram Sign In",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.header_label.pack(pady=(20, 5))

        self.sub_label = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=13),
            text_color="gray70",
            wraplength=380,
        )
        self.sub_label.pack(pady=(0, 15))

        # Entry
        self.entry_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.entry_frame.pack(fill="x", padx=40, pady=5)

        self.entry = ctk.CTkEntry(
            self.entry_frame,
            height=40,
            font=ctk.CTkFont(size=14),
        )
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", lambda e: self._on_submit())

        # Error label
        self.error_label = ctk.CTkLabel(
            self,
            text=error_message,
            text_color="#e74c3c",
            font=ctk.CTkFont(size=12),
            wraplength=380,
        )
        self.error_label.pack(pady=(5, 5))

        # Button frame
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=(15, 20), fill="x", padx=40)

        self.cancel_btn = ctk.CTkButton(
            btn_frame,
            text="Cancel",
            fg_color="gray35",
            hover_color="gray25",
            width=100,
            command=self._on_cancel,
        )
        self.cancel_btn.pack(side="left")

        self.submit_btn = ctk.CTkButton(
            btn_frame,
            text="Continue",
            width=140,
            command=self._on_submit,
        )
        self.submit_btn.pack(side="right")

        self._configure_mode()

    def _configure_mode(self):
        if self.mode == "phone":
            self.sub_label.configure(
                text="Enter your Telegram phone number with international country code (e.g. +1234567890):"
            )
            self.entry.configure(placeholder_text="+1234567890", show="")
            self.submit_btn.configure(text="Send Code")
        elif self.mode == "code":
            self.sub_label.configure(
                text=f"Enter the verification code Telegram sent to your account / phone ({self.phone}):"
            )
            self.entry.configure(placeholder_text="Enter 5-digit code", show="")
            self.submit_btn.configure(text="Verify Code")
        elif self.mode == "password":
            self.sub_label.configure(
                text="Two-Step Verification (2FA) is active. Enter your Telegram account password:"
            )
            self.entry.configure(placeholder_text="Account Password", show="*")
            self.submit_btn.configure(text="Sign In")
        self.entry.focus()

    def _on_submit(self):
        val = self.entry.get().strip()
        if not val:
            self.error_label.configure(text="Please enter a value.")
            return
        self.result = val
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class EngineController:
    """
    Manages the background asyncio event loop and executes engine tasks safely from Tkinter.
    """

    def __init__(self, log_queue: queue.Queue, ui_callback):
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.engine: Optional[SaveitEngine] = None
        self.is_running = False

    def start_loop(self):
        if self.thread and self.thread.is_alive():
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="SaveitAsyncLoop")
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def dispatch(self, coro):
        """Schedules a coroutine on the background event loop."""
        if not self.loop or not self.loop.is_running():
            self.start_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def log(self, level: str, message: str):
        self.log_queue.put((level, message))

    def update_status(self, status: str, details: dict):
        self.ui_callback("status_change", status=status, details=details)


class SaveitGUI(ctk.CTk):
    """
    Modern Windows GUI Application for Saveit.
    """

    def __init__(self):
        super().__init__()

        self.title("Saveit — Telegram Media Saver & Group Tutorial Forwarder")
        self.geometry("1100x720")
        self.minsize(980, 620)

        # Center on screen
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        start_x = max(0, (screen_w - 1100) // 2)
        start_y = max(0, (screen_h - 720) // 2)
        self.geometry(f"+{start_x}+{start_y}")

        self.log_queue = queue.Queue()
        self.controller = EngineController(self.log_queue, self._handle_controller_event)
        self.controller.start_loop()

        self.engine: Optional[SaveitEngine] = None
        self.bot_running = False
        self.batch_task_running = False
        self.joined_chats_cache: List[dict] = []

        # Load environment config
        self._ensure_env_file()
        load_dotenv(dotenv_path=ENV_PATH, override=True)

        self._init_layout()
        self._init_dashboard_tab()
        self._init_chat_explorer_tab()
        self._init_settings_tab()
        self._init_tracker_tab()
        self._init_about_tab()

        # Set default tab
        self._select_tab("dashboard")

        # Start background polling timers
        self.after(100, self._poll_logs)
        self.after(1000, self._refresh_dashboard_metrics)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _ensure_env_file(self):
        """Ensures .env exists, copying from .env.example if necessary."""
        if not ENV_PATH.exists() and ENV_EXAMPLE_PATH.exists():
            try:
                with open(ENV_EXAMPLE_PATH, "r", encoding="utf-8") as src, open(ENV_PATH, "w", encoding="utf-8") as dst:
                    dst.write(src.read())
            except Exception:
                pass

    def _init_layout(self):
        """Configures the primary grid layout with sidebar, main body, and status bar."""
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)  # Status bar
        self.grid_columnconfigure(0, weight=0)  # Sidebar
        self.grid_columnconfigure(1, weight=1)  # Main content view

        # ----------------- SIDEBAR -----------------
        self.sidebar_frame = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(6, weight=1)

        # App Brand
        brand_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="💾 Saveit",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        brand_label.grid(row=0, column=0, padx=20, pady=(20, 2), sticky="w")

        brand_sub = ctk.CTkLabel(
            self.sidebar_frame,
            text="Telegram Userbot",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
        )
        brand_sub.grid(row=1, column=0, padx=22, pady=(0, 20), sticky="w")

        # Navigation Buttons
        self.nav_buttons: Dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("dashboard", "🚀 Dashboard"),
            ("explorer", "💬 Chat Explorer"),
            ("settings", "⚙️ Settings"),
            ("tracker", "📊 Duplicate Tracker"),
            ("about", "ℹ️ Help & About"),
        ]

        for idx, (tab_id, label) in enumerate(nav_items, start=2):
            btn = ctk.CTkButton(
                self.sidebar_frame,
                text=label,
                height=38,
                corner_radius=8,
                anchor="w",
                font=ctk.CTkFont(size=13, weight="normal"),
                fg_color="transparent",
                text_color="gray85",
                hover_color=("gray75", "gray25"),
                command=lambda tid=tab_id: self._select_tab(tid),
            )
            btn.grid(row=idx, column=0, padx=12, pady=4, sticky="ew")
            self.nav_buttons[tab_id] = btn

        # Appearance switch in bottom sidebar
        mode_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="Appearance Mode:",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
        )
        mode_label.grid(row=7, column=0, padx=20, pady=(10, 0), sticky="w")

        self.mode_menu = ctk.CTkOptionMenu(
            self.sidebar_frame,
            values=["Dark", "Light", "System"],
            command=self._change_appearance_mode,
            height=28,
            font=ctk.CTkFont(size=12),
        )
        self.mode_menu.set("Dark")
        self.mode_menu.grid(row=8, column=0, padx=16, pady=(4, 15), sticky="ew")

        # ----------------- MAIN CONTENT AREA -----------------
        self.content_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.content_container.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        self.content_container.grid_rowconfigure(0, weight=1)
        self.content_container.grid_columnconfigure(0, weight=1)

        self.tabs: Dict[str, ctk.CTkFrame] = {}

        # ----------------- STATUS BAR -----------------
        self.status_bar = ctk.CTkFrame(self, height=32, corner_radius=0)
        self.status_bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.status_bar.grid_columnconfigure(1, weight=1)

        self.status_dot = ctk.CTkLabel(
            self.status_bar,
            text="●",
            text_color="#e74c3c",
            font=ctk.CTkFont(size=16),
        )
        self.status_dot.grid(row=0, column=0, padx=(15, 5), pady=2)

        self.status_text = ctk.CTkLabel(
            self.status_bar,
            text="Bot Stopped",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_text.grid(row=0, column=1, sticky="w", pady=2)

        self.account_badge = ctk.CTkLabel(
            self.status_bar,
            text="Account: Not Connected",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        self.account_badge.grid(row=0, column=2, padx=20, pady=2)

        self.db_stat_badge = ctk.CTkLabel(
            self.status_bar,
            text="Storage: 0 records (0.00 MB)",
            font=ctk.CTkFont(size=12),
            text_color="gray70",
        )
        self.db_stat_badge.grid(row=0, column=3, padx=(0, 20), pady=2)

    def _select_tab(self, tab_id: str):
        """Switches visible view tab and highlights corresponding sidebar button."""
        for tid, frame in self.tabs.items():
            frame.grid_forget()

        for tid, btn in self.nav_buttons.items():
            if tid == tab_id:
                btn.configure(
                    fg_color=("gray80", "#1f538d"),
                    text_color="white",
                    font=ctk.CTkFont(size=13, weight="bold"),
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color="gray85",
                    font=ctk.CTkFont(size=13, weight="normal"),
                )

        if tab_id in self.tabs:
            self.tabs[tab_id].grid(row=0, column=0, sticky="nsew")

    def _change_appearance_mode(self, mode: str):
        ctk.set_appearance_mode(mode)

    # -------------------------------------------------------------
    # 1. DASHBOARD TAB
    # -------------------------------------------------------------
    def _init_dashboard_tab(self):
        tab = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.tabs["dashboard"] = tab

        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Hero Banner / Bot Control
        hero = ctk.CTkFrame(tab, corner_radius=12)
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        hero.grid_columnconfigure(1, weight=1)

        self.bot_toggle_btn = ctk.CTkButton(
            hero,
            text="▶ Start Userbot",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=46,
            width=170,
            fg_color="#2ecc71",
            hover_color="#27ae60",
            command=self._toggle_bot,
        )
        self.bot_toggle_btn.grid(row=0, column=0, padx=20, pady=20)

        hero_text_frame = ctk.CTkFrame(hero, fg_color="transparent")
        hero_text_frame.grid(row=0, column=1, sticky="w", padx=10, pady=20)

        self.hero_status_label = ctk.CTkLabel(
            hero_text_frame,
            text="Userbot is currently idle",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.hero_status_label.pack(anchor="w")

        self.hero_sub_label = ctk.CTkLabel(
            hero_text_frame,
            text="Configure Telegram credentials in Settings or click Start to begin auto-saving.",
            font=ctk.CTkFont(size=12),
            text_color="gray65",
        )
        self.hero_sub_label.pack(anchor="w", pady=(2, 0))

        # Quick Metric Cards
        metrics_row = ctk.CTkFrame(tab, fg_color="transparent")
        metrics_row.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        metrics_row.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.card_records = self._create_metric_card(metrics_row, 0, "Archived Messages", "0")
        self.card_storage = self._create_metric_card(metrics_row, 1, "Archived Storage", "0.00 MB")
        self.card_hashes = self._create_metric_card(metrics_row, 2, "Unique Hashes", "0")
        self.card_monitored = self._create_metric_card(metrics_row, 3, "Monitored Groups", "0")

        # Live Log Console
        console_frame = ctk.CTkFrame(tab, corner_radius=12)
        console_frame.grid(row=2, column=0, sticky="nsew")
        console_frame.grid_rowconfigure(1, weight=1)
        console_frame.grid_columnconfigure(0, weight=1)

        # Console Header Toolbar
        console_bar = ctk.CTkFrame(console_frame, fg_color="transparent")
        console_bar.grid(row=0, column=0, sticky="ew", padx=15, pady=(12, 6))
        console_bar.grid_columnconfigure(1, weight=1)

        console_title = ctk.CTkLabel(
            console_bar,
            text="Activity Console & Live Logs",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        console_title.grid(row=0, column=0, sticky="w")

        # Filter Entry
        self.log_filter_entry = ctk.CTkEntry(
            console_bar,
            placeholder_text="Filter logs...",
            width=160,
            height=28,
            font=ctk.CTkFont(size=12),
        )
        self.log_filter_entry.grid(row=0, column=2, padx=(0, 10))

        self.auto_scroll_var = ctk.BooleanVar(value=True)
        auto_scroll_cb = ctk.CTkCheckBox(
            console_bar,
            text="Auto-scroll",
            variable=self.auto_scroll_var,
            font=ctk.CTkFont(size=12),
            height=24,
        )
        auto_scroll_cb.grid(row=0, column=3, padx=(0, 10))

        clear_btn = ctk.CTkButton(
            console_bar,
            text="Clear",
            width=65,
            height=28,
            fg_color="gray30",
            hover_color="gray20",
            font=ctk.CTkFont(size=12),
            command=self._clear_logs,
        )
        clear_btn.grid(row=0, column=4, padx=(0, 5))

        export_btn = ctk.CTkButton(
            console_bar,
            text="Save Log",
            width=80,
            height=28,
            fg_color="gray30",
            hover_color="gray20",
            font=ctk.CTkFont(size=12),
            command=self._export_logs,
        )
        export_btn.grid(row=0, column=5)

        # Log Text Box
        self.log_textbox = ctk.CTkTextbox(
            console_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.log_textbox.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 15))

        # Tag configurations for colors
        self.log_textbox.tag_config("info", foreground="#61afef")
        self.log_textbox.tag_config("success", foreground="#98c379")
        self.log_textbox.tag_config("warning", foreground="#e5c07b")
        self.log_textbox.tag_config("error", foreground="#e06c75")

    def _create_metric_card(self, parent, col, title, initial_value):
        card = ctk.CTkFrame(parent, corner_radius=10)
        card.grid(row=0, column=col, sticky="ew", padx=5)

        lbl_title = ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(size=12),
            text_color="gray65",
        )
        lbl_title.pack(padx=15, pady=(12, 2), anchor="w")

        lbl_val = ctk.CTkLabel(
            card,
            text=initial_value,
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl_val.pack(padx=15, pady=(0, 12), anchor="w")
        return lbl_val

    # -------------------------------------------------------------
    # 2. CHAT EXPLORER TAB
    # -------------------------------------------------------------
    def _init_chat_explorer_tab(self):
        tab = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.tabs["explorer"] = tab

        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Header toolbar
        tool_frame = ctk.CTkFrame(tab, corner_radius=10)
        tool_frame.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tool_frame.grid_columnconfigure(2, weight=1)

        fetch_btn = ctk.CTkButton(
            tool_frame,
            text="🔄 Scan Joined Groups & Channels",
            font=ctk.CTkFont(size=13, weight="bold"),
            height=36,
            command=self._trigger_fetch_dialogs,
        )
        fetch_btn.grid(row=0, column=0, padx=15, pady=12)

        self.chat_search_entry = ctk.CTkEntry(
            tool_frame,
            placeholder_text="Search by chat name, @username, or numeric ID...",
            height=36,
            font=ctk.CTkFont(size=13),
        )
        self.chat_search_entry.grid(row=0, column=2, sticky="ew", padx=10, pady=12)
        self.chat_search_entry.bind("<KeyRelease>", lambda e: self._render_chat_cards())

        # Active Batch Operation Progress Banner (hidden by default)
        self.batch_progress_frame = ctk.CTkFrame(tab, corner_radius=10, fg_color="#1f3b5c")
        self.batch_progress_frame.grid_columnconfigure(0, weight=1)

        self.batch_progress_label = ctk.CTkLabel(
            self.batch_progress_frame,
            text="Batch Operation Active",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.batch_progress_label.grid(row=0, column=0, sticky="w", padx=15, pady=(8, 2))

        self.batch_progressbar = ctk.CTkProgressBar(self.batch_progress_frame, mode="indeterminate", height=8)
        self.batch_progressbar.grid(row=1, column=0, sticky="ew", padx=15, pady=(2, 8))

        # Scrollable container for chats list
        self.chats_scroll = ctk.CTkScrollableFrame(tab, corner_radius=10)
        self.chats_scroll.grid(row=2, column=0, sticky="nsew")
        self.chats_scroll.grid_columnconfigure(0, weight=1)

        # Initial placeholder
        self.empty_chats_label = ctk.CTkLabel(
            self.chats_scroll,
            text="Click 'Scan Joined Groups & Channels' above to discover your Telegram chats.",
            font=ctk.CTkFont(size=14),
            text_color="gray60",
        )
        self.empty_chats_label.pack(pady=60)

    def _render_chat_cards(self):
        """Renders filtered joined chat cards into the scrollable list."""
        query = self.chat_search_entry.get().strip().lower()

        for widget in self.chats_scroll.winfo_children():
            widget.destroy()

        filtered = [
            c for c in self.joined_chats_cache
            if not query
            or query in str(c["id"]).lower()
            or query in str(c["title"]).lower()
            or query in str(c["username"]).lower()
        ]

        if not filtered:
            lbl = ctk.CTkLabel(
                self.chats_scroll,
                text="No matching groups or channels found.",
                font=ctk.CTkFont(size=14),
                text_color="gray60",
            )
            lbl.pack(pady=40)
            return

        for chat in filtered:
            card = ctk.CTkFrame(self.chats_scroll, corner_radius=8)
            card.pack(fill="x", pady=4, padx=5)
            card.grid_columnconfigure(1, weight=1)

            # Type badge
            type_color = "#3498db" if chat["type"] == "Channel" else "#9b59b6"
            badge = ctk.CTkLabel(
                card,
                text=chat["type"].upper(),
                fg_color=type_color,
                corner_radius=6,
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="white",
                width=80,
                height=24,
            )
            badge.grid(row=0, column=0, rowspan=2, padx=12, pady=10)

            # Title & Username
            title_text = chat["title"]
            user_text = f"Username: {chat['username']}" if chat['username'] != "-" else "Private / No Username"
            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.grid(row=0, column=1, rowspan=2, sticky="w", pady=8)

            t_lbl = ctk.CTkLabel(
                info_frame,
                text=title_text,
                font=ctk.CTkFont(size=14, weight="bold"),
            )
            t_lbl.pack(anchor="w")

            u_lbl = ctk.CTkLabel(
                info_frame,
                text=f"ID: {chat['id']}  •  {user_text}",
                font=ctk.CTkFont(size=11),
                text_color="gray60",
            )
            u_lbl.pack(anchor="w")

            # Actions
            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.grid(row=0, column=2, rowspan=2, padx=10, pady=8)

            copy_btn = ctk.CTkButton(
                btn_frame,
                text="📋 Copy ID",
                width=85,
                height=30,
                fg_color="gray30",
                hover_color="gray20",
                font=ctk.CTkFont(size=11),
                command=lambda cid=chat["id"]: self._copy_to_clipboard(str(cid)),
            )
            copy_btn.pack(side="left", padx=3)

            watch_btn = ctk.CTkButton(
                btn_frame,
                text="+ Watchlist",
                width=95,
                height=30,
                fg_color="#2980b9",
                hover_color="#1f618d",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda cid=chat["id"]: self._add_chat_to_monitored(str(cid)),
            )
            watch_btn.pack(side="left", padx=3)

            save_btn = ctk.CTkButton(
                btn_frame,
                text="⬇️ Save All Media",
                width=120,
                height=30,
                fg_color="#27ae60",
                hover_color="#219150",
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda c_entity=chat["id"], c_title=chat["title"]: self._trigger_batch_save(c_entity, c_title),
            )
            save_btn.pack(side="left", padx=3)

    def _copy_to_clipboard(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.controller.log("info", f"Copied ID to clipboard: {text}")

    def _add_chat_to_monitored(self, chat_id: str):
        """Adds a chat ID to FORWARD_GROUP_IDS in settings and .env."""
        current = self.forward_groups_entry.get().strip()
        groups = [g.strip() for g in current.split(",") if g.strip()]
        if chat_id not in groups:
            groups.append(chat_id)
            new_val = ",".join(groups)
            self.forward_groups_entry.delete(0, "end")
            self.forward_groups_entry.insert(0, new_val)
            self._save_settings(silent=True)
            self.controller.log("success", f"Added {chat_id} to monitored watchlist.")
        else:
            self.controller.log("info", f"{chat_id} is already in the monitored watchlist.")

    # -------------------------------------------------------------
    # 3. SETTINGS TAB
    # -------------------------------------------------------------
    def _init_settings_tab(self):
        tab = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.tabs["settings"] = tab

        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(tab, corner_radius=10)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        # Title
        hdr = ctk.CTkLabel(
            scroll,
            text="Application Settings (.env)",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        hdr.pack(anchor="w", padx=20, pady=(15, 10))

        # 1. API Credentials Group
        api_group = self._create_settings_group(scroll, "🔑 Telegram API Credentials")

        api_id_val = os.getenv("API_ID", "")
        self.api_id_entry = self._create_text_input(
            api_group, "API_ID (Numeric):", api_id_val, "e.g. 1234567"
        )

        api_hash_val = os.getenv("API_HASH", "")
        self.api_hash_entry = self._create_text_input(
            api_group, "API_HASH (Hex String):", api_hash_val, "e.g. 0123456789abcdef0123456789abcdef", show="*"
        )

        show_hash_cb = ctk.CTkCheckBox(
            api_group,
            text="Show API_HASH characters",
            font=ctk.CTkFont(size=12),
            command=lambda: self.api_hash_entry.configure(
                show="" if show_hash_cb.get() else "*"
            ),
        )
        show_hash_cb.pack(anchor="w", padx=15, pady=(2, 10))

        # 2. Automation Switches
        auto_group = self._create_settings_group(scroll, "⚡ Automation & Behavior")

        self.auto_timed_var = ctk.BooleanVar(value=os.getenv("AUTO_SAVE_TIMED", "true").lower() in {"1", "true", "yes", "on"})
        self._create_switch(auto_group, "Auto-save Timed / Disappearing Media", self.auto_timed_var, "Intercepts and preserves expiring photos and self-destruct videos immediately")

        self.media_only_var = ctk.BooleanVar(value=os.getenv("FORWARD_MEDIA_ONLY", "false").lower() in {"1", "true", "yes", "on"})
        self._create_switch(auto_group, "Media Only Filter", self.media_only_var, "Only forwards messages with downloadable files (skips pure text messages)")

        self.force_doc_var = ctk.BooleanVar(value=os.getenv("FORCE_DOCUMENT", "true").lower() in {"1", "true", "yes", "on"})
        self._create_switch(auto_group, "Force Document (Original Uncompressed Quality)", self.force_doc_var, "Uploads media as document files to prevent Telegram compression")

        self.cleanup_var = ctk.BooleanVar(value=os.getenv("CLEANUP_DOWNLOADS", "true").lower() in {"1", "true", "yes", "on"})
        self._create_switch(auto_group, "Cleanup Local Downloads", self.cleanup_var, "Deletes temporary downloaded media from disk after uploading to Saved Messages")

        # 3. Forwarding & Backfill
        fwd_group = self._create_settings_group(scroll, "🔄 Forwarding & Catch-Up")

        fwd_mode_val = os.getenv("FORWARD_MODE", "forward").lower()
        lbl_mode = ctk.CTkLabel(fwd_group, text="Forwarding Mode:", font=ctk.CTkFont(size=13, weight="bold"))
        lbl_mode.pack(anchor="w", padx=15, pady=(10, 2))

        self.forward_mode_seg = ctk.CTkSegmentedButton(
            fwd_group,
            values=["forward", "copy"],
        )
        self.forward_mode_seg.set(fwd_mode_val if fwd_mode_val in {"forward", "copy"} else "forward")
        self.forward_mode_seg.pack(anchor="w", padx=15, pady=(0, 10))

        fwd_groups_val = os.getenv("FORWARD_GROUP_IDS", "")
        self.forward_groups_entry = self._create_text_input(
            fwd_group, "Monitored Groups / Channels (comma-separated IDs or @usernames):", fwd_groups_val, "e.g. -1001234567890,@python_channel"
        )

        handler_val = os.getenv("HANDLER", ".saveit")
        self.handler_entry = self._create_text_input(
            fwd_group, "Userbot In-Chat Command Prefix:", handler_val, ".saveit"
        )

        backfill_val = os.getenv("BACKFILL_LIMIT", "0")
        self.backfill_entry = self._create_text_input(
            fwd_group, "Startup Catch-Up / Backfill Limit (0 = off, 50 = recent, 'all' = full history):", backfill_val, "0"
        )

        # 4. Anti-Ban & Rate Limiting
        rate_group = self._create_settings_group(scroll, "⏱️ Rate Limiting & Anti-Ban Protection")

        try:
            rate_val = float(os.getenv("RATE_LIMIT_DELAY", "1.5"))
        except ValueError:
            rate_val = 1.5

        lbl_rate = ctk.CTkLabel(rate_group, text=f"Rate Limit Delay: {rate_val:.1f}s per action", font=ctk.CTkFont(size=13, weight="bold"))
        lbl_rate.pack(anchor="w", padx=15, pady=(10, 2))

        self.rate_slider = ctk.CTkSlider(
            rate_group,
            from_=0.5,
            to=5.0,
            number_of_steps=45,
            command=lambda v: lbl_rate.configure(text=f"Rate Limit Delay: {v:.1f}s per action"),
        )
        self.rate_slider.set(rate_val)
        self.rate_slider.pack(fill="x", padx=15, pady=(0, 10))

        # Bottom Buttons
        btn_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(20, 30))

        save_btn = ctk.CTkButton(
            btn_frame,
            text="💾 Save Settings to .env",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=42,
            width=200,
            command=self._save_settings,
        )
        save_btn.pack(side="left", padx=(0, 10))

        reload_btn = ctk.CTkButton(
            btn_frame,
            text="🔄 Reload from .env",
            font=ctk.CTkFont(size=14),
            height=42,
            fg_color="gray30",
            hover_color="gray20",
            command=self._reload_settings,
        )
        reload_btn.pack(side="left")

    def _create_settings_group(self, parent, title):
        group = ctk.CTkFrame(parent, corner_radius=10)
        group.pack(fill="x", padx=20, pady=8)

        lbl = ctk.CTkLabel(
            group,
            text=title,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        lbl.pack(anchor="w", padx=15, pady=(12, 6))
        return group

    def _create_text_input(self, parent, label_text, default_val, placeholder="", show=""):
        lbl = ctk.CTkLabel(parent, text=label_text, font=ctk.CTkFont(size=12, weight="bold"))
        lbl.pack(anchor="w", padx=15, pady=(6, 2))

        entry = ctk.CTkEntry(
            parent,
            height=34,
            placeholder_text=placeholder,
            show=show,
            font=ctk.CTkFont(size=13),
        )
        entry.insert(0, default_val)
        entry.pack(fill="x", padx=15, pady=(0, 8))
        return entry

    def _create_switch(self, parent, title, variable, subtext=""):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=15, pady=6)

        sw = ctk.CTkSwitch(
            frame,
            text=title,
            variable=variable,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        sw.pack(anchor="w")

        if subtext:
            sub = ctk.CTkLabel(
                frame,
                text=subtext,
                font=ctk.CTkFont(size=11),
                text_color="gray60",
            )
            sub.pack(anchor="w", padx=45, pady=(2, 0))

    def _save_settings(self, silent=False):
        """Persists GUI input fields to the .env file."""
        try:
            if not ENV_PATH.exists():
                ENV_PATH.touch()

            set_key(str(ENV_PATH), "API_ID", self.api_id_entry.get().strip())
            set_key(str(ENV_PATH), "API_HASH", self.api_hash_entry.get().strip())
            set_key(str(ENV_PATH), "HANDLER", self.handler_entry.get().strip() or ".saveit")
            set_key(str(ENV_PATH), "AUTO_SAVE_TIMED", "true" if self.auto_timed_var.get() else "false")
            set_key(str(ENV_PATH), "FORWARD_GROUP_IDS", self.forward_groups_entry.get().strip())
            set_key(str(ENV_PATH), "FORWARD_MEDIA_ONLY", "true" if self.media_only_var.get() else "false")
            set_key(str(ENV_PATH), "FORWARD_MODE", self.forward_mode_seg.get())
            set_key(str(ENV_PATH), "FORCE_DOCUMENT", "true" if self.force_doc_var.get() else "false")
            set_key(str(ENV_PATH), "CLEANUP_DOWNLOADS", "true" if self.cleanup_var.get() else "false")
            set_key(str(ENV_PATH), "BACKFILL_LIMIT", self.backfill_entry.get().strip() or "0")
            set_key(str(ENV_PATH), "RATE_LIMIT_DELAY", f"{self.rate_slider.get():.1f}")

            load_dotenv(dotenv_path=ENV_PATH, override=True)

            if not silent:
                self.controller.log("success", "Settings saved successfully to .env.")
        except Exception as err:
            self.controller.log("error", f"Failed to save settings: {err}")

    def _reload_settings(self):
        """Reloads settings values from .env into GUI controls."""
        load_dotenv(dotenv_path=ENV_PATH, override=True)

        self.api_id_entry.delete(0, "end")
        self.api_id_entry.insert(0, os.getenv("API_ID", ""))

        self.api_hash_entry.delete(0, "end")
        self.api_hash_entry.insert(0, os.getenv("API_HASH", ""))

        self.handler_entry.delete(0, "end")
        self.handler_entry.insert(0, os.getenv("HANDLER", ".saveit"))

        self.auto_timed_var.set(os.getenv("AUTO_SAVE_TIMED", "true").lower() in {"1", "true", "yes", "on"})
        self.media_only_var.set(os.getenv("FORWARD_MEDIA_ONLY", "false").lower() in {"1", "true", "yes", "on"})
        self.force_doc_var.set(os.getenv("FORCE_DOCUMENT", "true").lower() in {"1", "true", "yes", "on"})
        self.cleanup_var.set(os.getenv("CLEANUP_DOWNLOADS", "true").lower() in {"1", "true", "yes", "on"})

        fwd_mode = os.getenv("FORWARD_MODE", "forward").lower()
        self.forward_mode_seg.set(fwd_mode if fwd_mode in {"forward", "copy"} else "forward")

        self.forward_groups_entry.delete(0, "end")
        self.forward_groups_entry.insert(0, os.getenv("FORWARD_GROUP_IDS", ""))

        self.backfill_entry.delete(0, "end")
        self.backfill_entry.insert(0, os.getenv("BACKFILL_LIMIT", "0"))

        try:
            r = float(os.getenv("RATE_LIMIT_DELAY", "1.5"))
            self.rate_slider.set(r)
        except ValueError:
            pass

        self.controller.log("info", "Settings reloaded from .env.")

    # -------------------------------------------------------------
    # 4. DUPLICATE TRACKER & STATS TAB
    # -------------------------------------------------------------
    def _init_tracker_tab(self):
        tab = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.tabs["tracker"] = tab

        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Overview Card
        top_card = ctk.CTkFrame(tab, corner_radius=10)
        top_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top_card.grid_columnconfigure(3, weight=1)

        btn_refresh = ctk.CTkButton(
            top_card,
            text="🔄 Refresh Stats",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34,
            command=self._refresh_tracker_view,
        )
        btn_refresh.grid(row=0, column=0, padx=15, pady=12)

        btn_vacuum = ctk.CTkButton(
            top_card,
            text="🧹 Compact / Vacuum DB",
            font=ctk.CTkFont(size=12),
            height=34,
            fg_color="gray30",
            hover_color="gray20",
            command=self._vacuum_database,
        )
        btn_vacuum.grid(row=0, column=1, padx=5, pady=12)

        btn_open_downloads = ctk.CTkButton(
            top_card,
            text="📂 Open Downloads Folder",
            font=ctk.CTkFont(size=12),
            height=34,
            fg_color="gray30",
            hover_color="gray20",
            command=self._open_downloads_folder,
        )
        btn_open_downloads.grid(row=0, column=2, padx=5, pady=12)

        # Recent Records Header
        rec_hdr = ctk.CTkLabel(
            tab,
            text="Recent Archived Media Records (SQLite)",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        rec_hdr.grid(row=1, column=0, sticky="w", pady=(5, 6))

        # Records Table / List
        self.tracker_records_scroll = ctk.CTkScrollableFrame(tab, corner_radius=10)
        self.tracker_records_scroll.grid(row=2, column=0, sticky="nsew")
        self.tracker_records_scroll.grid_columnconfigure(0, weight=1)

        self._refresh_tracker_view()

    def _refresh_tracker_view(self):
        """Fetches and displays the recent 50 saved records from SQLite."""
        db_path = os.getenv("TRACKER_DB", "saveit_tracker.db")
        tracker = FileTracker(db_path)
        records = tracker.get_recent_records(limit=50)

        for w in self.tracker_records_scroll.winfo_children():
            w.destroy()

        if not records:
            lbl = ctk.CTkLabel(
                self.tracker_records_scroll,
                text="No saved records found in database yet.",
                font=ctk.CTkFont(size=14),
                text_color="gray60",
            )
            lbl.pack(pady=50)
            return

        for r in records:
            row_frame = ctk.CTkFrame(self.tracker_records_scroll, corner_radius=6)
            row_frame.pack(fill="x", pady=3, padx=5)
            row_frame.grid_columnconfigure(1, weight=1)

            # File size calculation
            size_b = r.get("file_size") or 0
            size_str = (
                f"{size_b / (1024 * 1024):.1f} MB"
                if size_b >= 1024 * 1024
                else f"{size_b / 1024:.1f} KB"
            )

            ts = r.get("saved_at", "")[:19]
            fname = r.get("file_name") or r.get("caption") or f"Message {r.get('message_id')}"
            fhash = (r.get("file_hash") or "")[:12]
            fhash_str = f"SHA: {fhash}..." if fhash else "No hash (text)"

            lbl_time = ctk.CTkLabel(
                row_frame,
                text=ts,
                font=ctk.CTkFont(size=11),
                text_color="gray60",
                width=130,
            )
            lbl_time.grid(row=0, column=0, padx=8, pady=6)

            lbl_name = ctk.CTkLabel(
                row_frame,
                text=fname[:60],
                font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            )
            lbl_name.grid(row=0, column=1, sticky="w", padx=8, pady=6)

            lbl_meta = ctk.CTkLabel(
                row_frame,
                text=f"{size_str}  •  Chat: {r.get('chat_id')}  •  {fhash_str}",
                font=ctk.CTkFont(size=11),
                text_color="gray60",
            )
            lbl_meta.grid(row=0, column=2, padx=12, pady=6)

    def _vacuum_database(self):
        """Compacts SQLite file."""
        db_path = os.getenv("TRACKER_DB", "saveit_tracker.db")
        tracker = FileTracker(db_path)
        tracker.vacuum()
        self.controller.log("success", "SQLite database vacuum completed successfully.")
        self._refresh_tracker_view()

    def _open_downloads_folder(self):
        """Opens downloads directory in file explorer."""
        p = Path("downloads")
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)])
        else:
            subprocess.run(["xdg-open", str(p)])

    # -------------------------------------------------------------
    # 5. HELP & ABOUT TAB
    # -------------------------------------------------------------
    def _init_about_tab(self):
        tab = ctk.CTkFrame(self.content_container, fg_color="transparent")
        self.tabs["about"] = tab

        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(tab, corner_radius=10)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        # Title
        t_lbl = ctk.CTkLabel(
            scroll,
            text="Saveit — Telegram Timed Media Saver & Tutorial Forwarder",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        t_lbl.pack(anchor="w", padx=20, pady=(20, 5))

        desc = (
            "Saveit is an automated Telegram userbot that preserves disappearing media, "
            "intercepts self-destructing photos/videos, and auto-forwards lessons, code tutorials, "
            "and materials from your monitored groups directly into your Saved Messages.\n\n"
            "Key Features:\n"
            "• Timed Media Preservation: Intercepts self-destructing media immediately upon arrival.\n"
            "• Restricted Content Bypass: Falls back to re-uploading original files if forward is restricted.\n"
            "• Persistent Duplicate Prevention: SQLite tracker checks File IDs and cryptographic SHA-256 hashes.\n"
            "• Adaptive Anti-Ban Protection: Human-like randomized jitter and rate limiting (FloodWait shielding).\n"
            "• Historical Backfill: Easily catch up past lessons or batch save entire chat media histories."
        )
        d_lbl = ctk.CTkLabel(
            scroll,
            text=desc,
            font=ctk.CTkFont(size=13),
            text_color="gray80",
            justify="left",
            wraplength=760,
        )
        d_lbl.pack(anchor="w", padx=20, pady=5)

        # Telegram credentials guide
        guide_group = self._create_settings_group(scroll, "📖 Getting Your Telegram API Credentials")
        guide_text = (
            "1. Visit https://my.telegram.org and sign in with your phone number.\n"
            "2. Click on 'API development tools'.\n"
            "3. Fill out the application form (e.g. App title: Saveit, Short name: saveit).\n"
            "4. Copy your numeric api_id and 32-character api_hash.\n"
            "5. Paste them into the Settings tab of this application and click Save."
        )
        g_lbl = ctk.CTkLabel(
            guide_group,
            text=guide_text,
            font=ctk.CTkFont(size=12),
            text_color="gray75",
            justify="left",
        )
        g_lbl.pack(anchor="w", padx=15, pady=(5, 12))

        # In-Chat Commands Reference
        cmd_group = self._create_settings_group(scroll, "💬 In-Chat Userbot Commands")
        cmd_text = (
            "• .saveit (as reply to media) — Manually saves the replied media to Saved Messages\n"
            "• .id / .chatid — Shows numeric Chat ID, username, and type of the current chat\n"
            "• .stats — Displays duplicate tracker statistics and total archived storage\n"
            "• .rate [seconds] — Views or adjusts the rate limiter delay dynamically\n"
            "• .savehere [limit|all] — Batch saves media from the current chat to Saved Messages\n"
            "• .savegroup <chat_id> [limit|all] — Batch saves media from specified group"
        )
        c_lbl = ctk.CTkLabel(
            cmd_group,
            text=cmd_text,
            font=ctk.CTkFont(size=12),
            text_color="gray75",
            justify="left",
        )
        c_lbl.pack(anchor="w", padx=15, pady=(5, 12))

    # -------------------------------------------------------------
    # BOT CONTROLLER & ASYNC LIFECYCLE
    # -------------------------------------------------------------
    def _toggle_bot(self):
        """Starts or stops the Saveit Userbot."""
        if not self.bot_running:
            self._start_bot()
        else:
            self._stop_bot()

    def _start_bot(self):
        """Validates credentials and launches SaveitEngine on background loop."""
        api_id = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")

        if not api_id or not api_hash or api_id == "YOUR_API_ID":
            self.controller.log("error", "API_ID and API_HASH are not configured. Please set them in Settings.")
            self._select_tab("settings")
            return

        self.bot_toggle_btn.configure(
            text="⏳ Starting...",
            fg_color="gray40",
            state="disabled",
        )
        self.hero_status_label.configure(text="Connecting to Telegram...")
        self.status_dot.configure(text_color="#f39c12")
        self.status_text.configure(text="Connecting...")

        # Setup authentication callbacks
        auth_callbacks = {
            "request_phone": self._async_request_phone,
            "request_code": self._async_request_code,
            "request_password": self._async_request_password,
        }

        self.engine = SaveitEngine(
            api_id=api_id,
            api_hash=api_hash,
            handler=os.getenv("HANDLER", ".saveit"),
            auto_save_timed=os.getenv("AUTO_SAVE_TIMED", "true").lower() in {"1", "true", "yes", "on"},
            forward_groups=os.getenv("FORWARD_GROUP_IDS", ""),
            forward_media_only=os.getenv("FORWARD_MEDIA_ONLY", "false").lower() in {"1", "true", "yes", "on"},
            forward_mode=os.getenv("FORWARD_MODE", "forward").lower(),
            force_document=os.getenv("FORCE_DOCUMENT", "true").lower() in {"1", "true", "yes", "on"},
            cleanup_downloads=os.getenv("CLEANUP_DOWNLOADS", "true").lower() in {"1", "true", "yes", "on"},
            backfill_limit=os.getenv("BACKFILL_LIMIT", "0"),
            rate_limit_delay=float(os.getenv("RATE_LIMIT_DELAY", "1.5")),
            flood_sleep_threshold=int(os.getenv("FLOOD_SLEEP_THRESHOLD", "60")),
            tracker_db=os.getenv("TRACKER_DB", "saveit_tracker.db"),
            log_callback=self.controller.log,
            status_callback=self.controller.update_status,
            auth_callbacks=auth_callbacks,
        )

        async def _run():
            try:
                await self.engine.start()
                await self.engine.run_until_stopped()
            except Exception as e:
                self.controller.log("error", f"Engine stopped with error: {e}")
                self.controller.update_status("ERROR", message=str(e))

        self.controller.dispatch(_run())

    def _stop_bot(self):
        """Halts the SaveitEngine gracefully."""
        self.bot_toggle_btn.configure(
            text="⏳ Stopping...",
            fg_color="gray40",
            state="disabled",
        )
        if self.engine:
            self.controller.dispatch(self.engine.stop())

    # Authentication async bridges (dispatched from engine to Tkinter main thread)
    async def _async_request_phone(self) -> str:
        fut = asyncio.get_event_loop().create_future()

        def _open():
            dialog = AuthDialog(self, mode="phone")
            self.wait_window(dialog)
            if not fut.done():
                if dialog.result:
                    fut.set_result(dialog.result)
                else:
                    fut.set_exception(RuntimeError("Sign-in cancelled by user."))

        self.after(0, _open)
        return await fut

    async def _async_request_code(self, phone: str) -> str:
        fut = asyncio.get_event_loop().create_future()

        def _open():
            dialog = AuthDialog(self, mode="code", phone=phone)
            self.wait_window(dialog)
            if not fut.done():
                if dialog.result:
                    fut.set_result(dialog.result)
                else:
                    fut.set_exception(RuntimeError("Verification cancelled by user."))

        self.after(0, _open)
        return await fut

    async def _async_request_password(self) -> str:
        fut = asyncio.get_event_loop().create_future()

        def _open():
            dialog = AuthDialog(self, mode="password")
            self.wait_window(dialog)
            if not fut.done():
                if dialog.result:
                    fut.set_result(dialog.result)
                else:
                    fut.set_exception(RuntimeError("2FA cancelled by user."))

        self.after(0, _open)
        return await fut

    # Event router from background thread to UI
    def _handle_controller_event(self, event_type: str, **kwargs):
        self.after(0, lambda: self._process_controller_event(event_type, kwargs))

    def _process_controller_event(self, event_type: str, kwargs: dict):
        if event_type == "status_change":
            status = kwargs.get("status")
            details = kwargs.get("details", {})

            if status == "RUNNING":
                self.bot_running = True
                self.bot_toggle_btn.configure(
                    text="⏹ Stop Userbot",
                    fg_color="#e74c3c",
                    hover_color="#c0392b",
                    state="normal",
                )
                self.status_dot.configure(text_color="#2ecc71")
                self.status_text.configure(text="Bot Running & Listening")
                self.hero_status_label.configure(text="Userbot is Active")
                self.hero_sub_label.configure(text="Monitoring chats and auto-saving timed media in real-time.")

            elif status == "AUTHENTICATED":
                user = details.get("user", "User")
                uid = details.get("user_id", "")
                self.account_badge.configure(text=f"Account: {user} ({uid})")

            elif status == "STOPPED":
                self.bot_running = False
                self.bot_toggle_btn.configure(
                    text="▶ Start Userbot",
                    fg_color="#2ecc71",
                    hover_color="#27ae60",
                    state="normal",
                )
                self.status_dot.configure(text_color="#e74c3c")
                self.status_text.configure(text="Bot Stopped")
                self.hero_status_label.configure(text="Userbot is currently idle")
                self.hero_sub_label.configure(text="Click Start to begin auto-saving.")

            elif status == "RECONNECTING":
                self.status_dot.configure(text_color="#f39c12")
                self.status_text.configure(text="Reconnecting...")

            elif status == "ERROR":
                self.bot_running = False
                self.bot_toggle_btn.configure(
                    text="▶ Start Userbot",
                    fg_color="#2ecc71",
                    hover_color="#27ae60",
                    state="normal",
                )
                self.status_dot.configure(text_color="#e74c3c")
                self.status_text.configure(text="Error Encountered")
                self.hero_status_label.configure(text="Userbot Stopped with Error")
                self.hero_sub_label.configure(text=str(details.get("message", "Unknown error")))

    # -------------------------------------------------------------
    # CHAT EXPLORER ACTIONS
    # -------------------------------------------------------------
    def _trigger_fetch_dialogs(self):
        """Fetches joined Telegram dialogs."""
        if not self.engine:
            api_id = os.getenv("API_ID")
            api_hash = os.getenv("API_HASH")
            if not api_id or not api_hash:
                self.controller.log("error", "API_ID and API_HASH required to fetch dialogs.")
                self._select_tab("settings")
                return

            self.engine = SaveitEngine(
                api_id=api_id,
                api_hash=api_hash,
                log_callback=self.controller.log,
                status_callback=self.controller.update_status,
                auth_callbacks={
                    "request_phone": self._async_request_phone,
                    "request_code": self._async_request_code,
                    "request_password": self._async_request_password,
                },
            )

        self.controller.log("info", "Scanning joined groups and channels from Telegram...")

        async def _fetch():
            try:
                chats = await self.engine.get_dialogs_list()
                self.joined_chats_cache = chats
                self.after(0, self._render_chat_cards)
                self.controller.log("success", f"Discovered {len(chats)} groups and channels.")
            except Exception as e:
                self.controller.log("error", f"Failed to fetch chats: {e}")

        self.controller.dispatch(_fetch())

    def _trigger_batch_save(self, chat_entity: Union[int, str], chat_title: str):
        """Runs batch save for all media in a specific chat."""
        if self.batch_task_running:
            self.controller.log("warning", "A batch operation is already running.")
            return

        self.batch_task_running = True
        self.batch_progress_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.batch_progressbar.start()
        self.batch_progress_label.configure(text=f"Scanning & Saving ALL media from '{chat_title}'...")

        self.controller.log("info", f"[Batch Save] Initiating media extraction for '{chat_title}' ({chat_entity})...")

        async def _batch():
            try:
                def progress(processed, saved, done):
                    def _update():
                        if not done:
                            self.batch_progress_label.configure(
                                text=f"'{chat_title}': Scanned {processed} messages | Saved {saved} media files..."
                            )
                        else:
                            self.batch_progressbar.stop()
                            self.batch_progress_frame.grid_forget()
                            self.batch_task_running = False
                            self.controller.log("success", f"[Batch Save] Completed '{chat_title}': Saved {saved} media files.")
                    self.after(0, _update)

                await self.engine.batch_save_chat_messages(
                    chat_entity,
                    limit=None,
                    media_only=True,
                    progress_callback=progress,
                )
            except Exception as e:
                self.controller.log("error", f"Batch save failed: {e}")
                self.after(0, lambda: (self.batch_progressbar.stop(), self.batch_progress_frame.grid_forget()))
                self.batch_task_running = False

        self.controller.dispatch(_batch())

    # -------------------------------------------------------------
    # LOGS & METRICS POLLING
    # -------------------------------------------------------------
    def _poll_logs(self):
        """Pulls log items from the queue and inserts them into the log console."""
        filter_str = self.log_filter_entry.get().strip().lower()
        items = []
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                items.append((level, msg))
        except queue.Empty:
            pass

        if items:
            for level, msg in items:
                if filter_str and filter_str not in msg.lower():
                    continue

                tag = "info"
                if "error" in level:
                    tag = "error"
                elif "warn" in level:
                    tag = "warning"
                elif "succ" in level:
                    tag = "success"

                self.log_textbox.insert("end", msg + "\n", tag)

            if self.auto_scroll_var.get():
                self.log_textbox.see("end")

        self.after(100, self._poll_logs)

    def _clear_logs(self):
        self.log_textbox.delete("1.0", "end")

    def _export_logs(self):
        """Saves current logs to a text file."""
        content = self.log_textbox.get("1.0", "end")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_file = Path(f"saveit_logs_{ts}.txt")
        try:
            export_file.write_text(content, encoding="utf-8")
            self.controller.log("success", f"Logs exported to {export_file.name}")
        except Exception as e:
            self.controller.log("error", f"Failed to export logs: {e}")

    def _refresh_dashboard_metrics(self):
        """Periodically refreshes dashboard card numbers from SQLite."""
        try:
            db_path = os.getenv("TRACKER_DB", "saveit_tracker.db")
            tracker = FileTracker(db_path)
            stats = tracker.get_stats()

            total_rec = stats.get("total_records", 0)
            total_b = stats.get("total_bytes", 0)
            hashes = stats.get("unique_hashes", 0)

            mb = total_b / (1024 * 1024)
            gb = mb / 1024
            size_str = f"{gb:.2f} GB" if gb >= 1.0 else f"{mb:.2f} MB"

            fwd_str = os.getenv("FORWARD_GROUP_IDS", "")
            monitored_cnt = len(parse_group_targets(fwd_str))

            self.card_records.configure(text=f"{total_rec:,}")
            self.card_storage.configure(text=size_str)
            self.card_hashes.configure(text=f"{hashes:,}")
            self.card_monitored.configure(text=str(monitored_cnt))

            self.db_stat_badge.configure(text=f"Storage: {total_rec:,} records ({size_str})")
        except Exception:
            pass

        self.after(3000, self._refresh_dashboard_metrics)

    def _on_close(self):
        """Shuts down background loop and destroys window."""
        if self.engine and self.engine.is_running:
            self.controller.dispatch(self.engine.stop())
        self.destroy()


def main():
    app = SaveitGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
