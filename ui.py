"""Tkinter GUI for the Gmail outreach + auto-reply campaign."""

import csv
import json
import logging
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import campaign_state
import google_auth
import message_store
import paths
from campaign_state import CampaignState
from exceptions import AuthenticationError, ConfigurationError
from gmail_client import (
    Credentials,
    GmailClient,
    SendResult,
    build_client,
    clear_credentials,
    load_credentials,
    normalize_app_password,
    save_credentials,
)
from imap_client import build_inbox
from message_store import MessageStore
from sender import Campaign, CampaignCallbacks, deduplicate_emails, validate_campaign

CONFIG_PATH = paths.CONFIG_PATH
LOG_DIR = paths.LOG_DIR

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PUMP_INTERVAL_MS = 50

# Display statuses (a superset of the persisted ones — Sending/Replied are transient).
STATUS_PENDING = "Pending"
STATUS_SENDING = "Sending"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"
STATUS_REPLIED = "Replied"
STATUS_DONE = "Done"

STATUS_TAGS = {
    STATUS_PENDING: "pending",
    STATUS_SENDING: "sending",
    STATUS_SENT: "sent",
    STATUS_FAILED: "failed",
    STATUS_REPLIED: "replied",
    STATUS_DONE: "done",
}

THEMES = {
    "light": {
        "bg": "#f5f5f5", "fg": "#212529", "text_bg": "#ffffff", "text_fg": "#212529",
        "insert": "#212529", "select_bg": "#cce5ff", "disabled_bg": "#e9ecef",
    },
    "dark": {
        "bg": "#2b2b2b", "fg": "#e0e0e0", "text_bg": "#3c3c3c", "text_fg": "#e0e0e0",
        "insert": "#ffffff", "select_bg": "#4a6fa5", "disabled_bg": "#323232",
    },
}

DEFAULT_CONFIG = {
    "interval_seconds": 30,
    "poll_interval_seconds": 60,
    "theme": "light",
}


def create_app_icon() -> tk.PhotoImage:
    """Build a simple mail envelope icon using tkinter PhotoImage."""
    size = 64
    img = tk.PhotoImage(width=size, height=size)
    green, white = "#28a745", "#ffffff"
    for y in range(size):
        row = []
        for x in range(size):
            in_body = 14 <= x <= 50 and 28 <= y <= 46
            in_flap = 14 <= x <= 50 and 20 <= y <= 30 and y <= 24 + abs(x - 32) * 0.35
            in_seal = 28 <= x <= 36 and 38 <= y <= 44
            row.append(white if (in_body or in_flap or in_seal) else green)
        img.put("{" + " ".join(row) + "}", to=(0, y))
    return img


def load_csv_emails(parent: tk.Misc) -> list[str] | None:
    """Open a CSV file and return valid emails from the 'email' column."""
    path = filedialog.askopenfilename(
        parent=parent,
        title="Select CSV file",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
    )
    if not path:
        return None

    valid_emails: list[str] = []
    seen: set[str] = set()
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                messagebox.showerror("Load CSV", "The CSV file is empty or has no header row.")
                return None
            email_key = next(
                (n for n in reader.fieldnames if n.strip().lower() == "email"), None
            )
            if email_key is None:
                messagebox.showerror("Load CSV", "CSV must contain a column named 'email'.")
                return None
            for row in reader:
                address = (row.get(email_key) or "").strip()
                if EMAIL_PATTERN.match(address) and address.lower() not in seen:
                    valid_emails.append(address)
                    seen.add(address.lower())
    except OSError as exc:
        messagebox.showerror("Load CSV", f"Could not read file:\n{exc}")
        return None

    if not valid_emails:
        messagebox.showwarning("Load CSV", "No valid email addresses found in the 'email' column.")
        return []
    messagebox.showinfo("Load CSV", f"Loaded {len(valid_emails)} valid emails")
    return valid_emails


def format_duration(seconds: float) -> str:
    """Seconds -> a short 'Xh Ym' / 'Ym' / '<1m' string."""
    seconds = max(0, int(seconds))
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def format_cooldown(seconds: float) -> str:
    return "Ready" if seconds <= 0 else f"Locked {format_duration(seconds)}"


class SignInDialog(tk.Toplevel):
    """Sign in to Gmail — one click with Google, or an App Password as a fallback."""

    def __init__(self, parent: tk.Misc, initial: Credentials | None = None):
        super().__init__(parent)
        self.title("Sign in to Gmail")
        self.geometry("470x430")
        self.resizable(False, False)
        self.transient(parent)
        self.client: GmailClient | None = None
        self.inbox = None
        self.imap_error: str | None = None
        self._closed = False

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Sign in to Gmail", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky=tk.W, pady=(0, 10)
        )

        self.google_btn = tk.Button(
            frame, text="Sign in with Google", command=self._google_sign_in,
            font=("Segoe UI", 10, "bold"), bg="#1a73e8", fg="white",
            activebackground="#1765cc", activeforeground="white",
            relief=tk.FLAT, cursor="hand2", pady=8,
        )
        self.google_btn.grid(row=1, column=0, sticky=tk.EW)
        ttk.Label(
            frame,
            text="Opens your browser. Nothing to copy or paste, and it stays signed in.",
            font=("Segoe UI", 8), foreground="#888",
        ).grid(row=2, column=0, sticky=tk.W, pady=(4, 12))

        separator = ttk.Frame(frame)
        separator.grid(row=3, column=0, sticky=tk.EW, pady=(0, 12))
        separator.columnconfigure((0, 2), weight=1)
        ttk.Separator(separator).grid(row=0, column=0, sticky=tk.EW, pady=6)
        ttk.Label(separator, text="  or use an App Password  ", font=("Segoe UI", 8),
                  foreground="#888").grid(row=0, column=1)
        ttk.Separator(separator).grid(row=0, column=2, sticky=tk.EW, pady=6)

        ttk.Label(frame, text="Email address:").grid(row=4, column=0, sticky=tk.W, pady=(0, 4))
        self.email_var = tk.StringVar(value=initial.email if initial else "")
        email_entry = ttk.Entry(frame, textvariable=self.email_var)
        email_entry.grid(row=5, column=0, sticky=tk.EW, pady=(0, 10))

        ttk.Label(frame, text="App password:").grid(row=6, column=0, sticky=tk.W, pady=(0, 4))
        self.password_var = tk.StringVar(
            value=initial.app_password if initial and not initial.uses_oauth else ""
        )
        password_entry = ttk.Entry(frame, textvariable=self.password_var, show="•")
        password_entry.grid(row=7, column=0, sticky=tk.EW, pady=(0, 10))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Remember on this computer", variable=self.remember_var).grid(
            row=8, column=0, sticky=tk.W, pady=(0, 8)
        )

        ttk.Label(
            frame,
            text=(
                "Either way, IMAP must be ON (Gmail → Settings → Forwarding and POP/IMAP)\n"
                "so the app can detect replies."
            ),
            font=("Segoe UI", 8), foreground="#888", justify=tk.LEFT,
        ).grid(row=9, column=0, sticky=tk.W, pady=(0, 10))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 9)).grid(
            row=10, column=0, sticky=tk.W
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, sticky=tk.EW, pady=(12, 0))
        self.cancel_btn = ttk.Button(buttons, text="Cancel", command=self.destroy)
        self.cancel_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self.connect_btn = ttk.Button(buttons, text="Connect", command=self._connect)
        self.connect_btn.pack(side=tk.RIGHT)

        self.bind("<Return>", lambda _e: self._connect())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        (self.google_btn if not self.email_var.get() else password_entry).focus_set()
        self.grab_set()
        self.wait_window()

    def destroy(self) -> None:
        # The OAuth worker thread polls back into this window; tell it we are gone.
        self._closed = True
        super().destroy()

    def _set_busy(self, busy: bool, status: str = "") -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for button in (self.google_btn, self.connect_btn):
            button.configure(state=state)
        self.status_var.set(status)
        self.update_idletasks()

    # ------------------------------------------------------------------ google

    def _google_sign_in(self) -> None:
        try:
            config = google_auth.load_client_config()
        except ConfigurationError as exc:
            messagebox.showinfo("Google sign-in — one-time setup", str(exc), parent=self)
            return

        self._set_busy(True, "Waiting for you to finish in the browser...")
        result: dict = {}

        def work() -> None:
            try:
                email, token = google_auth.sign_in(config)
                result["credentials"] = Credentials(email=email, oauth=token)
            except (AuthenticationError, ConfigurationError) as exc:
                result["error"] = str(exc)

        threading.Thread(target=work, daemon=True).start()
        self._await_google(result)

    def _await_google(self, result: dict) -> None:
        """Poll the worker without blocking the Tk event loop (the browser wait is long)."""
        if self._closed:
            return
        if not result:
            self.after(150, self._await_google, result)
            return
        if "error" in result:
            self._set_busy(False)
            messagebox.showerror("Google sign-in failed", result["error"], parent=self)
            return
        self._finish(result["credentials"])

    # ------------------------------------------------------------ app password

    def _connect(self) -> None:
        email = self.email_var.get().strip()
        password = normalize_app_password(self.password_var.get())
        if not email or not password:
            messagebox.showwarning(
                "Sign in",
                "Email and app password are required.\n\n"
                "Or click 'Sign in with Google' and skip the password entirely.",
                parent=self,
            )
            return
        self._finish(Credentials(email=email, app_password=password))

    # ----------------------------------------------------------------- shared

    def _finish(self, credentials: Credentials) -> None:
        """Verify the credentials against SMTP (and IMAP), save them, and close."""
        self._set_busy(True, "Connecting to Gmail (SMTP)...")
        try:
            client = build_client(credentials)
        except (AuthenticationError, ConfigurationError) as exc:
            self._set_busy(False)
            messagebox.showerror("Sign in failed", str(exc), parent=self)
            return

        self._set_busy(True, "Checking inbox access (IMAP)...")
        try:
            self.inbox = build_inbox(credentials)
        except (AuthenticationError, ConfigurationError) as exc:
            self.inbox = None
            self.imap_error = str(exc)

        if self.remember_var.get():
            try:
                save_credentials(credentials)
            except ConfigurationError as exc:
                messagebox.showwarning("Sign in", str(exc), parent=self)
        else:
            clear_credentials()

        self.client = client
        self.destroy()


class FirstMessageDialog(tk.Toplevel):
    """Create or edit one first-message (subject + body)."""

    def __init__(self, parent: tk.Misc, subject: str = "", body: str = "", title: str = "First message"):
        super().__init__(parent)
        self.title(title)
        self.geometry("520x420")
        self.transient(parent)
        self.result: tuple[str, str] | None = None

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(frame, text="Subject:", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky=tk.W, pady=(0, 4)
        )
        self.subject_var = tk.StringVar(value=subject)
        subject_entry = ttk.Entry(frame, textvariable=self.subject_var, font=("Segoe UI", 10))
        subject_entry.grid(row=1, column=0, sticky=tk.EW, pady=(0, 10))

        ttk.Label(frame, text="Body:", font=("Segoe UI", 10, "bold")).grid(
            row=2, column=0, sticky=tk.W, pady=(0, 4)
        )
        self.body_text = scrolledtext.ScrolledText(frame, height=12, wrap=tk.WORD, font=("Segoe UI", 10))
        self.body_text.grid(row=3, column=0, sticky=tk.NSEW)
        self.body_text.insert("1.0", body)

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky=tk.EW, pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT)

        subject_entry.focus_set()
        self.grab_set()
        self.wait_window()

    def _save(self) -> None:
        subject = self.subject_var.get().strip()
        body = self.body_text.get("1.0", tk.END).strip()
        if not subject or not body:
            messagebox.showwarning("First message", "Both subject and body are required.", parent=self)
            return
        self.result = (subject, body)
        self.destroy()


BULK_PLACEHOLDER = """Quick follow-up on your Q3 rollout
Hi there,

I saw the announcement and wanted to reach out.

Best,
Me
---
Question about your hiring plans
Hi there,

Different message, different subject — same pool.

Best,
Me"""


class BulkImportDialog(tk.Toplevel):
    """Add many first messages at once: paste them, or load a CSV or a folder."""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("Import first messages")
        self.geometry("620x520")
        self.transient(parent)
        self.result: list[tuple[str, str]] = []

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Label(
            frame,
            text=(
                "Paste your messages below. Separate them with a line of three dashes (---).\n"
                "In each message the first line is the subject and the rest is the body."
            ),
            justify=tk.LEFT,
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))

        sources = ttk.Frame(frame)
        sources.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        ttk.Button(sources, text="Load CSV…", command=self._load_csv).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(sources, text="Load folder…", command=self._load_folder).pack(side=tk.LEFT)
        ttk.Label(
            sources,
            text="CSV needs subject + body columns. A folder takes each .txt/.md file as one message.",
            font=("Segoe UI", 8), foreground="#888",
        ).pack(side=tk.LEFT, padx=(10, 0))

        self.text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Segoe UI", 10), undo=True)
        self.text.grid(row=2, column=0, sticky=tk.NSEW)
        self.text.insert("1.0", BULK_PLACEHOLDER)
        # The sample is a template, not content: the first keystroke replaces it, so a
        # user can type over it without selecting-all first.
        self.text.tag_add(tk.SEL, "1.0", tk.END)
        self.text.bind("<<Modified>>", self._on_modified)

        self.count_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.count_var, style="Progress.TLabel").grid(
            row=3, column=0, sticky=tk.W, pady=(6, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky=tk.EW, pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(buttons, text="Import", command=self._import_text).pack(side=tk.RIGHT)

        self._recount()
        self.text.focus_set()
        self.grab_set()
        self.wait_window()

    def _on_modified(self, _event=None) -> None:
        # Tk latches this flag and stops reporting until it is cleared.
        self.text.edit_modified(False)
        self._recount()

    def _recount(self) -> None:
        found = len(message_store.parse_bulk_text(self.text.get("1.0", tk.END)))
        self.count_var.set(f"{found} message{'' if found == 1 else 's'} detected")

    def _accept(self, drafts: list[tuple[str, str]], source: str) -> None:
        if not drafts:
            messagebox.showwarning(
                "Import", f"No usable messages found in {source}.", parent=self
            )
            return
        self.result = drafts
        self.destroy()

    def _import_text(self) -> None:
        self._accept(message_store.parse_bulk_text(self.text.get("1.0", tk.END)), "the pasted text")

    def _load_csv(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Import messages from CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            drafts = message_store.parse_csv_file(Path(path))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            messagebox.showerror("Import", f"Could not read the CSV:\n{exc}", parent=self)
            return
        self._accept(drafts, Path(path).name)

    def _load_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Import messages from a folder")
        if not folder:
            return
        try:
            drafts = message_store.parse_folder(Path(folder))
        except OSError as exc:
            messagebox.showerror("Import", f"Could not read the folder:\n{exc}", parent=self)
            return
        self._accept(drafts, Path(folder).name)


class GmailAutoSenderApp:
    """Main application window."""

    def __init__(self, root: tk.Misc | None = None):
        self.root = root if root is not None else tk.Tk()
        self.root.title("Gmail Auto Sender")
        self.root.geometry("1040x820")
        self.root.minsize(920, 700)

        self.config = self._load_config()
        self._setup_logging()
        self._setup_styles()
        self._setup_icon()

        self._store = MessageStore(message_store.MESSAGES_PATH)
        self._state = CampaignState(campaign_state.STATE_PATH)
        self._client: GmailClient | None = None
        self._inbox = None
        self._campaign: Campaign | None = None

        self._ui_queue: queue.Queue = queue.Queue()
        self._pump_id: str | None = None
        self._closing = False

        self._build_ui()
        self._apply_theme(self.config.get("theme", "light"))
        self._refresh_first_list()
        self._load_second_into_editor()
        self._refresh_recipients_table()
        self._refresh_start_button()
        self._update_control_states("idle")
        self._pump()
        self._restore_saved_session()
        self._announce_resumable()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ ui thread

    def _post(self, callback) -> None:
        self._ui_queue.put(callback)

    def _pump(self) -> None:
        if self._closing:
            return
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                return
        if not self._closing:
            self._pump_id = self.root.after(PUMP_INTERVAL_MS, self._pump)

    # -------------------------------------------------------------- config

    def _load_config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open(encoding="utf-8") as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    config.update(stored)
            except (OSError, json.JSONDecodeError):
                pass
        return config

    def _save_config(self) -> None:
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            messagebox.showerror("Settings", f"Could not save config.json:\n{exc}")

    def _setup_icon(self) -> None:
        self._app_icon = create_app_icon()
        self.root.iconphoto(True, self._app_icon)

    def _setup_logging(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        logging.basicConfig(
            filename=LOG_DIR / "app.log",
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    def _setup_styles(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10))
        style.configure("Progress.TLabel", font=("Segoe UI", 9))

    def _apply_theme(self, theme_name: str) -> None:
        theme = THEMES.get(theme_name, THEMES["light"])
        self.config["theme"] = theme_name
        self.root.configure(bg=theme["bg"])
        style = ttk.Style()
        style.configure(".", background=theme["bg"], foreground=theme["fg"])
        for name in (
            "TFrame", "TLabel", "Title.TLabel", "Section.TLabel", "Status.TLabel",
            "Progress.TLabel", "TLabelframe", "TLabelframe.Label",
        ):
            style.configure(name, background=theme["bg"], foreground=theme["fg"])
        style.configure("TEntry", fieldbackground=theme["text_bg"], foreground=theme["text_fg"])
        style.configure(
            "Treeview", background=theme["text_bg"], foreground=theme["text_fg"],
            fieldbackground=theme["text_bg"],
        )
        style.configure("Treeview.Heading", background=theme["bg"], foreground=theme["fg"])

        self._paste_colors = (theme["text_bg"], theme["disabled_bg"])
        for widget in (self.log_text, self.paste_text, self.second_text):
            widget.configure(
                bg=theme["text_bg"], fg=theme["text_fg"],
                insertbackground=theme["insert"], selectbackground=theme["select_bg"],
            )

        for tree in (self.first_tree, self.recipients_tree):
            tree.tag_configure("pending", foreground=theme["fg"])
            tree.tag_configure("sending", foreground="#007bff")
            tree.tag_configure("sent", foreground="#17a2b8")
            tree.tag_configure("failed", foreground="#dc3545")
            tree.tag_configure("replied", foreground="#6f42c1")
            tree.tag_configure("done", foreground="#28a745")
            tree.tag_configure("ready", foreground="#28a745")
            tree.tag_configure("locked", foreground="#fd7e14")

        self.theme_btn.configure(text="Light Mode" if theme_name == "dark" else "Dark Mode")

    def _toggle_theme(self) -> None:
        self._apply_theme("dark" if self.config.get("theme", "light") == "light" else "light")
        self._save_config()

    # ------------------------------------------------------------------ auth

    def _restore_saved_session(self) -> None:
        credentials = load_credentials()
        if credentials is None:
            self._append_log("No saved credentials. Click Sign in to connect.", "info")
            self.status_var.set("Not signed in")
            return
        self.status_var.set(f"Reconnecting as {credentials.email}...")
        self.root.update_idletasks()
        try:
            client = build_client(credentials)
        except (AuthenticationError, ConfigurationError) as exc:
            self.status_var.set("Not signed in")
            self._append_log(f"Saved credentials rejected: {exc}", "fail")
            return
        inbox = None
        try:
            inbox = build_inbox(credentials)
        except (AuthenticationError, ConfigurationError) as exc:
            self._append_log(f"IMAP unavailable — replies can't be detected yet: {exc}", "fail")
        self._set_session(client, inbox)

    def _set_session(self, client: GmailClient, inbox) -> None:
        self._client = client
        self._inbox = inbox
        self.account_var.set(f"Signed in: {client.email}")
        imap_note = "" if inbox is not None else "  (IMAP off — reply detection disabled)"
        self.status_var.set(f"Connected as {client.email}{imap_note}")
        self._append_log(f"Signed in as {client.email}.", "success")
        if inbox is None:
            self._append_log("IMAP is off; enable it in Gmail to auto-send replies.", "fail")

    def _announce_resumable(self) -> None:
        if not self._state.has_resumable_work():
            return
        counts = self._state.counts()
        contacted = counts[campaign_state.STATUS_SENT] + counts[campaign_state.STATUS_DONE]
        total = len(self._state.recipients)
        self._append_log(
            f"Campaign in progress: {contacted}/{total} contacted, {counts[campaign_state.STATUS_DONE]} "
            f"replied. Click Resume campaign to continue.",
            "info",
        )
        self.status_var.set("Campaign paused — click Resume campaign to continue.")

    def _sign_in(self) -> bool:
        if self._campaign and self._campaign.is_running:
            messagebox.showwarning("Sign in", "Stop the campaign before changing accounts.")
            return False
        dialog = SignInDialog(self.root, initial=load_credentials())
        if dialog.client is None:
            return False
        if dialog.imap_error:
            messagebox.showwarning("IMAP not available", dialog.imap_error)
        if self._client is not None:
            self._client.close()
        if self._inbox is not None:
            self._inbox.close()
        self._set_session(dialog.client, dialog.inbox)
        return True

    def _sign_out(self) -> None:
        if self._campaign and self._campaign.is_running:
            messagebox.showwarning("Sign out", "Stop the campaign before signing out.")
            return
        if self._client is not None:
            self._client.close()
        if self._inbox is not None:
            self._inbox.close()
        self._client = None
        self._inbox = None
        clear_credentials()
        self.account_var.set("Not signed in")
        self.status_var.set("Signed out")
        self._append_log("Signed out and removed saved credentials.", "info")

    def _ensure_login(self) -> bool:
        if self._client is not None and self._client.is_logged_in():
            return True
        return self._sign_in()

    # -------------------------------------------------------------- settings

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.geometry("400x260")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Send interval between first messages (seconds):",
                  style="Section.TLabel").grid(row=0, column=0, sticky=tk.W, pady=(0, 4))
        interval_var = tk.IntVar(value=int(self.config.get("interval_seconds", 30)))
        ttk.Spinbox(frame, from_=5, to=3600, increment=5, textvariable=interval_var, width=10).grid(
            row=1, column=0, sticky=tk.W, pady=(0, 12)
        )

        ttk.Label(frame, text="Inbox check interval for replies (seconds):",
                  style="Section.TLabel").grid(row=2, column=0, sticky=tk.W, pady=(0, 4))
        poll_var = tk.IntVar(value=int(self.config.get("poll_interval_seconds", 60)))
        ttk.Spinbox(frame, from_=15, to=3600, increment=15, textvariable=poll_var, width=10).grid(
            row=3, column=0, sticky=tk.W, pady=(0, 12)
        )

        def save_settings() -> None:
            try:
                interval = int(interval_var.get())
                poll = int(poll_var.get())
            except tk.TclError:
                messagebox.showwarning("Settings", "Enter valid numbers.", parent=dialog)
                return
            if not 5 <= interval <= 3600 or not 15 <= poll <= 3600:
                messagebox.showwarning("Settings", "Values out of range.", parent=dialog)
                return
            self.config["interval_seconds"] = interval
            self.config["poll_interval_seconds"] = poll
            self.interval_label_var.set(f"Interval: {interval}s  ·  Poll: {poll}s")
            self._save_config()
            self._append_log(f"Interval {interval}s, poll {poll}s.", "info")
            dialog.destroy()

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=4, column=0, sticky=tk.EW)
        ttk.Button(btn_row, text="Save", command=save_settings).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
        dialog.wait_window()

    # -------------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(container)
        header.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(header, text="Gmail Auto Sender", style="Title.TLabel").pack(side=tk.LEFT)
        self.account_var = tk.StringVar(value="Not signed in")
        ttk.Label(header, textvariable=self.account_var, style="Status.TLabel").pack(
            side=tk.LEFT, padx=(16, 0)
        )
        toolbar = ttk.Frame(header)
        toolbar.pack(side=tk.RIGHT)
        self.theme_btn = ttk.Button(toolbar, text="Dark Mode", command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(toolbar, text="Settings", command=self._open_settings).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(toolbar, text="Sign out", command=self._sign_out).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(toolbar, text="Sign in", command=self._sign_in).pack(side=tk.RIGHT, padx=(6, 0))

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        self._build_message_panel(content)
        self._build_recipients_panel(content)
        self._build_controls(container)
        self._build_log(container)

    def _build_message_panel(self, parent: tk.Misc) -> None:
        panel = ttk.Frame(parent)
        panel.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=3)
        panel.rowconfigure(1, weight=2)

        first = ttk.LabelFrame(
            panel, text="First messages — one unique message per recipient (24h lock)", padding=10
        )
        first.grid(row=0, column=0, sticky=tk.NSEW, pady=(0, 8))
        first.columnconfigure(0, weight=1)
        first.rowconfigure(0, weight=1)

        tree_wrap = ttk.Frame(first)
        tree_wrap.grid(row=0, column=0, sticky=tk.NSEW)
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)
        self.first_tree = ttk.Treeview(
            tree_wrap, columns=("subject", "state"), show="headings", selectmode="extended", height=6
        )
        self.first_tree.heading("subject", text="Subject")
        self.first_tree.heading("state", text="Availability")
        self.first_tree.column("subject", width=260, anchor=tk.W)
        self.first_tree.column("state", width=110, anchor=tk.CENTER, stretch=False)
        self.first_tree.grid(row=0, column=0, sticky=tk.NSEW)
        self.first_tree.bind("<Double-1>", lambda _e: self._edit_first())
        self.first_tree.bind("<Delete>", lambda _e: self._delete_first())
        scroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.first_tree.yview)
        self.first_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky=tk.NS)

        first_btns = ttk.Frame(first)
        first_btns.grid(row=1, column=0, sticky=tk.EW, pady=(6, 0))
        self.first_import_btn = ttk.Button(first_btns, text="Import…", command=self._import_first)
        self.first_import_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.first_new_btn = ttk.Button(first_btns, text="New", command=self._new_first)
        self.first_new_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.first_edit_btn = ttk.Button(first_btns, text="Edit", command=self._edit_first)
        self.first_edit_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.first_dup_btn = ttk.Button(first_btns, text="Duplicate", command=self._duplicate_first)
        self.first_dup_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.first_del_btn = ttk.Button(first_btns, text="Delete", command=self._delete_first)
        self.first_del_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.first_reset_btn = ttk.Button(first_btns, text="Clear locks", command=self._reset_locks)
        self.first_reset_btn.pack(side=tk.LEFT)
        self.first_count_var = tk.StringVar(value="")
        ttk.Label(first_btns, textvariable=self.first_count_var, style="Progress.TLabel").pack(side=tk.RIGHT)

        second = ttk.LabelFrame(
            panel, text="Second message — auto-reply after they respond (body only)", padding=10
        )
        second.grid(row=1, column=0, sticky=tk.NSEW)
        second.columnconfigure(0, weight=1)
        second.rowconfigure(0, weight=1)
        self.second_text = scrolledtext.ScrolledText(
            second, height=6, wrap=tk.WORD, font=("Segoe UI", 10), relief=tk.FLAT, borderwidth=1
        )
        self.second_text.grid(row=0, column=0, sticky=tk.NSEW)
        second_btns = ttk.Frame(second)
        second_btns.grid(row=1, column=0, sticky=tk.EW, pady=(6, 0))
        self.second_save_btn = ttk.Button(second_btns, text="Save reply", command=self._save_second)
        self.second_save_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.second_clear_btn = ttk.Button(second_btns, text="Clear", command=self._clear_second)
        self.second_clear_btn.pack(side=tk.LEFT)
        ttk.Label(
            second_btns, text="Sent as a real Reply — subject becomes \"Re: …\" automatically.",
            style="Progress.TLabel",
        ).pack(side=tk.RIGHT)

    def _build_recipients_panel(self, parent: tk.Misc) -> None:
        panel = ttk.LabelFrame(parent, text="Recipients", padding=10)
        panel.grid(row=0, column=1, sticky=tk.NSEW, padx=(8, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)

        ttk.Label(panel, text="Paste emails (one per line), then Import", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 4)
        )
        import_bar = ttk.Frame(panel)
        import_bar.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        import_bar.columnconfigure(0, weight=1)
        self.paste_text = scrolledtext.ScrolledText(
            import_bar, height=4, wrap=tk.NONE, font=("Consolas", 9), relief=tk.FLAT, borderwidth=1
        )
        self.paste_text.grid(row=0, column=0, sticky=tk.EW, columnspan=4, pady=(0, 4))
        self.import_btn = ttk.Button(import_bar, text="Import", command=self._import_recipients_from_paste)
        self.import_btn.grid(row=1, column=0, sticky=tk.W, padx=(0, 4))
        self.csv_btn = ttk.Button(import_bar, text="Load CSV", command=self._load_csv)
        self.csv_btn.grid(row=1, column=1, sticky=tk.W, padx=(0, 4))
        self.delete_btn = ttk.Button(import_bar, text="Delete Selected", command=self._delete_selected_recipients)
        self.delete_btn.grid(row=1, column=2, sticky=tk.W, padx=(0, 4))
        self.clear_btn = ttk.Button(import_bar, text="Clear All", command=self._clear_recipients)
        self.clear_btn.grid(row=1, column=3, sticky=tk.W)

        tree_frame = ttk.Frame(panel)
        tree_frame.grid(row=2, column=0, sticky=tk.NSEW)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.recipients_tree = ttk.Treeview(
            tree_frame, columns=("num", "email", "status"), show="headings",
            selectmode="extended", height=14,
        )
        self.recipients_tree.bind("<Delete>", lambda _e: self._delete_selected_recipients())
        for col, head, width, anchor, stretch in (
            ("num", "#", 40, tk.CENTER, False),
            ("email", "Email", 230, tk.W, True),
            ("status", "Status", 90, tk.CENTER, False),
        ):
            self.recipients_tree.heading(col, text=head)
            self.recipients_tree.column(col, width=width, anchor=anchor, stretch=stretch)
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.recipients_tree.yview)
        self.recipients_tree.configure(yscrollcommand=scroll.set)
        self.recipients_tree.grid(row=0, column=0, sticky=tk.NSEW)
        scroll.grid(row=0, column=1, sticky=tk.NS)

        self.recipient_stats_var = tk.StringVar(value="Total: 0 | Contacted: 0 | Done: 0 | Pending: 0 | Failed: 0")
        ttk.Label(panel, textvariable=self.recipient_stats_var, style="Progress.TLabel").grid(
            row=3, column=0, sticky=tk.W, pady=(6, 0)
        )
        ttk.Label(
            panel,
            text="The ▸ marker is the resume point — the next recipient to be contacted.",
            style="Progress.TLabel",
        ).grid(row=4, column=0, sticky=tk.W, pady=(4, 0))

    def _build_controls(self, parent: tk.Misc) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill=tk.X, pady=12)
        btns = ttk.Frame(controls)
        btns.pack(side=tk.LEFT)

        def action(text, command, bg, active, fg="white", afg="white"):
            return tk.Button(
                btns, text=text, command=command, bg=bg, fg=fg, activebackground=active,
                activeforeground=afg, font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                padx=20, pady=8, cursor="hand2",
            )

        self.send_btn = action("Start campaign", self.start, "#28a745", "#218838")
        self.send_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_btn = action("Stop", self.stop, "#dc3545", "#c82333")
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.pause_btn = action("Pause", self.pause, "#ffc107", "#e0a800", fg="#212529", afg="#212529")
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.resume_btn = action("Resume", self.resume, "#007bff", "#0069d9")
        self.resume_btn.pack(side=tk.LEFT)

        self.interval_label_var = tk.StringVar(
            value=f"Interval: {self.config.get('interval_seconds', 30)}s  ·  "
                  f"Poll: {self.config.get('poll_interval_seconds', 60)}s"
        )
        ttk.Label(controls, textvariable=self.interval_label_var, style="Progress.TLabel").pack(side=tk.RIGHT)

        progress = ttk.Frame(parent)
        progress.pack(fill=tk.X, pady=(0, 8))
        progress.columnconfigure(0, weight=1)
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(progress, variable=self.progress_var, maximum=100, mode="determinate").grid(
            row=0, column=0, sticky=tk.EW
        )
        self.progress_label_var = tk.StringVar(value="0/0 contacted")
        ttk.Label(progress, textvariable=self.progress_label_var, style="Progress.TLabel").grid(
            row=0, column=1, padx=(10, 0)
        )

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(parent, textvariable=self.status_var, style="Status.TLabel").pack(anchor=tk.W, pady=(0, 8))

    def _build_log(self, parent: tk.Misc) -> None:
        panel = ttk.LabelFrame(parent, text="Live Log", padding=10)
        panel.pack(fill=tk.BOTH, expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            panel, height=8, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED,
            relief=tk.FLAT, borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        self.log_text.tag_configure("success", foreground="#28a745")
        self.log_text.tag_configure("fail", foreground="#dc3545")
        self.log_text.tag_configure("info", foreground="#777777")

    # -------------------------------------------------- first-message CRUD

    def _refresh_first_list(self) -> None:
        selected_ids = set(self.first_tree.selection())
        for item in self.first_tree.get_children():
            self.first_tree.delete(item)
        for message in self._store.first_pool:
            preview = (message.subject or "(no subject)")[:60]
            state = format_cooldown(message.cooldown_remaining())
            tag = "ready" if message.is_available() else "locked"
            self.first_tree.insert("", tk.END, iid=message.id, values=(preview, state), tags=(tag,))
        surviving = [i for i in self.first_tree.get_children() if i in selected_ids]
        if surviving:
            self.first_tree.selection_set(surviving)
        available = self._store.available_count()
        self.first_count_var.set(f"{len(self._store.first_pool)} messages · {available} ready")

    def _selected_first_id(self) -> str | None:
        """The one selected message, for the actions that only make sense on one."""
        selection = self.first_tree.selection()
        return selection[0] if selection else None

    def _selected_first_ids(self) -> tuple[str, ...]:
        return self.first_tree.selection()

    def _new_first(self) -> None:
        dialog = FirstMessageDialog(self.root, title="New first message")
        if dialog.result:
            subject, body = dialog.result
            self._store.add_first(subject, body)
            self._refresh_first_list()
            self._append_log(f'Added first message: "{subject[:40]}"', "info")

    def _import_first(self) -> None:
        dialog = BulkImportDialog(self.root)
        if not dialog.result:
            return
        added = self._store.add_many_first(dialog.result)
        self._refresh_first_list()
        self._append_log(f"Imported {added} first message{'' if added == 1 else 's'}.", "success")

    def _duplicate_first(self) -> None:
        message_id = self._selected_first_id()
        if not message_id:
            messagebox.showwarning("Duplicate", "Select a message to duplicate.")
            return
        copy = self._store.duplicate_first(message_id)
        if copy is None:
            self._refresh_first_list()
            return
        self._refresh_first_list()
        self.first_tree.selection_set(copy.id)
        self._append_log(f'Duplicated first message: "{copy.subject[:40]}"', "info")

    def _edit_first(self) -> None:
        message_id = self._selected_first_id()
        if not message_id:
            messagebox.showwarning("Edit", "Select a message to edit.")
            return
        message = self._store.get_first(message_id)
        if message is None:
            self._refresh_first_list()
            return
        dialog = FirstMessageDialog(
            self.root, subject=message.subject, body=message.body, title="Edit first message"
        )
        if dialog.result:
            subject, body = dialog.result
            self._store.update_first(message_id, subject, body)
            self._refresh_first_list()
            self._append_log(f'Edited first message: "{subject[:40]}"', "info")

    def _delete_first(self) -> None:
        message_ids = self._selected_first_ids()
        if not message_ids:
            messagebox.showwarning("Delete", "Select one or more messages to delete.")
            return

        if len(message_ids) == 1:
            message = self._store.get_first(message_ids[0])
            label = message.subject[:40] if message else message_ids[0]
            prompt = f'Delete first message "{label}"?'
        else:
            prompt = f"Delete {len(message_ids)} first messages?"
        if not messagebox.askyesno("Delete", prompt):
            return

        removed = self._store.delete_many_first(message_ids)
        self._refresh_first_list()
        self._append_log(f"Deleted {removed} first message{'' if removed == 1 else 's'}.", "info")

    def _reset_locks(self) -> None:
        if not messagebox.askyesno(
            "Clear locks", "Clear all 24h locks so every message is available again?"
        ):
            return
        self._store.reset_cooldowns()
        self._refresh_first_list()
        self._append_log("Cleared all 24h message locks.", "info")

    # --------------------------------------------------------- second message

    def _load_second_into_editor(self) -> None:
        self.second_text.delete("1.0", tk.END)
        self.second_text.insert("1.0", self._store.second_body)

    def _save_second(self) -> None:
        body = self.second_text.get("1.0", tk.END).strip()
        if not body:
            messagebox.showwarning("Reply message", "The reply body is empty.")
            return
        self._store.set_second(body)
        self._append_log("Saved the second (reply) message.", "info")

    def _clear_second(self) -> None:
        if not messagebox.askyesno("Clear", "Clear the second (reply) message?"):
            return
        self._store.clear_second()
        self._load_second_into_editor()
        self._append_log("Cleared the second (reply) message.", "info")

    # ------------------------------------------------------------ recipients

    def _extract_emails(self, text: str) -> list[str]:
        emails = []
        for line in text.splitlines():
            for candidate in re.split(r"[,;\s]+", line):
                candidate = candidate.strip().strip("\"'<>")
                if candidate and EMAIL_PATTERN.match(candidate):
                    emails.append(candidate)
        return emails

    def _recipient_key(self, email: str) -> str:
        return email.strip().lower()

    def _is_running(self) -> bool:
        return self._campaign is not None and self._campaign.is_running

    def _refresh_recipients_table(self) -> None:
        for item in self.recipients_tree.get_children():
            self.recipients_tree.delete(item)
        cursor = self._state.cursor
        for index, record in enumerate(self._state.recipients):
            key = self._recipient_key(record.email)
            marker = "▸" if (index == cursor and self._state.active) else ""
            tag = STATUS_TAGS.get(record.status, "pending")
            self.recipients_tree.insert(
                "", tk.END, iid=key, values=(f"{marker}{index + 1}", record.email, record.status),
                tags=(tag,),
            )
        self._refresh_recipient_stats()

    def _clear_recipients(self) -> None:
        if self._is_running():
            messagebox.showwarning("Recipients", "Stop the campaign before clearing the list.")
            return
        if not self._state.recipients:
            return
        if not messagebox.askyesno(
            "Clear All", "Remove all recipients and reset this campaign's progress?"
        ):
            return
        self._state.clear()
        self._refresh_recipients_table()
        self._refresh_start_button()
        self._append_log("Cleared all recipients and reset campaign progress.", "info")

    def _delete_selected_recipients(self) -> None:
        if self._is_running():
            messagebox.showwarning("Delete", "Stop the campaign before deleting recipients.")
            return
        selected = self.recipients_tree.selection()
        if not selected:
            messagebox.showwarning(
                "Delete", "Select one or more recipients to delete.\n\n"
                "Use Ctrl+click to select multiple, or Shift+click for a range.",
            )
            return
        count = len(selected)
        prompt = (
            f"Delete {self._state.get(selected[0]).email}?" if count == 1
            else f"Delete {count} selected recipients?"
        )
        if not messagebox.askyesno("Delete Recipients", prompt):
            return
        self._state.remove_emails(list(selected))
        # Removing rows shifts the cursor; keep it pointing at an un-contacted slot.
        self._state.cursor = min(self._state.cursor, len(self._state.recipients))
        self._state.save()
        self._refresh_recipients_table()
        self._refresh_start_button()
        self._append_log(f"Deleted {count} recipient(s).", "info")

    def _add_recipients(self, emails: list[str]) -> int:
        unique, duplicates = deduplicate_emails(emails)
        added = self._state.add_emails(unique)
        self._refresh_recipients_table()
        self._refresh_start_button()
        if duplicates:
            self._append_log(f"Skipped {duplicates} duplicate address(es) in the paste.", "info")
        return added

    def _get_recipient_emails(self) -> list[str]:
        return self._state.emails()

    def _set_recipient_row(self, email: str, status: str) -> None:
        """Update one row's visible status (transient states like Sending/Replied)."""
        key = self._recipient_key(email)
        children = self.recipients_tree.get_children()
        if key not in children:
            return
        index = children.index(key)
        marker = "▸" if (index == self._state.cursor and self._state.active) else ""
        self.recipients_tree.item(
            key, values=(f"{marker}{index + 1}", email, status),
            tags=(STATUS_TAGS.get(status, "pending"),),
        )
        self.recipients_tree.see(key)

    def _refresh_recipient_stats(self) -> None:
        counts = self._state.counts()
        total = len(self._state.recipients)
        contacted = counts[campaign_state.STATUS_SENT] + counts[campaign_state.STATUS_DONE]
        self.recipient_stats_var.set(
            f"Total: {total} | Contacted: {contacted} | Done: {counts[campaign_state.STATUS_DONE]} "
            f"| Pending: {counts[campaign_state.STATUS_PENDING]} | Failed: {counts[campaign_state.STATUS_FAILED]}"
        )

    def _import_recipients_from_paste(self) -> None:
        if self._is_running():
            messagebox.showwarning("Import", "Stop the campaign before importing recipients.")
            return
        raw = self._extract_emails(self.paste_text.get("1.0", tk.END))
        if not raw:
            messagebox.showwarning("Import", "No valid email addresses found.")
            return
        added = self._add_recipients(raw)
        self.paste_text.delete("1.0", tk.END)
        self.paste_text.focus_set()
        if added:
            self._append_log(f"Imported {added} recipient(s).", "info")
        else:
            messagebox.showinfo("Import", "All addresses are already in the list.")

    def _load_csv(self) -> None:
        if self._is_running():
            messagebox.showwarning("Load CSV", "Stop the campaign before importing recipients.")
            return
        emails = load_csv_emails(self.root)
        if not emails:
            return
        added = self._add_recipients(emails)
        if added:
            self._append_log(f"Added {added} recipient(s) from CSV.", "info")

    # ----------------------------------------------------------------- logs

    def _session_log_path(self) -> Path:
        return LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.log"

    def _write_session_log(self, message: str) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            with self._session_log_path().open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {message}\n")
        except OSError:
            pass

    def _append_log(self, message: str, tag: str = "info") -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self._write_session_log(message)

    # ------------------------------------------------------------- controls

    def _refresh_start_button(self) -> None:
        self.send_btn.configure(
            text="Resume campaign" if self._state.has_resumable_work() else "Start campaign"
        )

    def _update_control_states(self, state: str) -> None:
        sending = state == "sending"
        paused = state == "paused"
        idle = state == "idle"

        self.send_btn.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self.stop_btn.configure(state=tk.DISABLED if idle else tk.NORMAL)
        self.pause_btn.configure(state=tk.NORMAL if sending else tk.DISABLED)
        self.resume_btn.configure(state=tk.NORMAL if paused else tk.DISABLED)

        editing_buttons = (
            self.import_btn, self.csv_btn, self.delete_btn, self.clear_btn,
            self.first_import_btn, self.first_new_btn, self.first_edit_btn,
            self.first_dup_btn, self.first_del_btn, self.first_reset_btn,
            self.second_save_btn, self.second_clear_btn,
        )
        for button in editing_buttons:
            button.configure(state=tk.NORMAL if idle else tk.DISABLED)

        normal_bg, disabled_bg = getattr(self, "_paste_colors", ("#ffffff", "#e9ecef"))
        for widget in (self.paste_text, self.second_text):
            widget.configure(
                state=tk.NORMAL if idle else tk.DISABLED,
                bg=normal_bg if idle else disabled_bg,
            )

    def _update_progress(self, current: int, total: int) -> None:
        self.progress_label_var.set(f"{current}/{total} contacted")
        self.progress_var.set((current / total) * 100 if total else 0)

    # ------------------------------------------------------ campaign events

    def _campaign_callbacks(self) -> CampaignCallbacks:
        return CampaignCallbacks(
            on_first_sending=self._ev_first_sending,
            on_first_result=self._ev_first_result,
            on_waiting=self._ev_waiting,
            on_phase_watch=self._ev_watch,
            on_reply_detected=self._ev_reply,
            on_second_result=self._ev_second_result,
            on_complete=self._ev_complete,
        )

    def _ev_first_sending(self, email: str) -> None:
        def update() -> None:
            self._set_recipient_row(email, STATUS_SENDING)
            self.status_var.set(f"Sending first message to {email}...")
        self._post(update)

    def _ev_first_result(self, result: SendResult, cursor: int, total: int) -> None:
        def update() -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            if result.success:
                self._append_log(f"✅ First message sent to {result.email} — {ts}", "success")
                self._set_recipient_row(result.email, STATUS_SENT)
            else:
                self._append_log(f"❌ First message failed: {result.email} — {ts} ({result.error})", "fail")
                self._set_recipient_row(result.email, STATUS_FAILED)
            self._update_progress(cursor, total)
            self._refresh_recipient_stats()
            self._refresh_first_list()
        self._post(update)

    def _ev_waiting(self, seconds: float, contacted: int, total: int) -> None:
        def update() -> None:
            self.status_var.set(
                f"Batch done — {contacted}/{total} contacted. Waiting ~{format_duration(seconds)} "
                f"for message locks; the next batch resumes automatically (even if you close the app)."
            )
            self._refresh_recipients_table()  # move the ▸ resume marker
        self._post(update)

    def _ev_watch(self, awaiting: int, answered: int) -> None:
        self._post(lambda: self.status_var.set(
            f"Watching inbox for replies — {awaiting} awaiting, {answered} answered..."
        ))

    def _ev_reply(self, email: str) -> None:
        def update() -> None:
            self._append_log(f"📨 Reply received from {email} — sending follow-up...", "info")
            self._set_recipient_row(email, STATUS_REPLIED)
        self._post(update)

    def _ev_second_result(self, result: SendResult) -> None:
        def update() -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            if result.success:
                self._append_log(f"✅ Reply sent to {result.email} — {ts}", "success")
                self._set_recipient_row(result.email, STATUS_DONE)
            else:
                self._append_log(f"❌ Reply failed: {result.email} — {ts} ({result.error})", "fail")
                self._set_recipient_row(result.email, STATUS_SENT)
            self._refresh_recipient_stats()
        self._post(update)

    def _ev_complete(self, stopped: bool) -> None:
        def finish() -> None:
            if stopped:
                self.status_var.set("Stopped — click Resume campaign to continue later.")
                self._append_log("Campaign stopped. Progress saved; resume anytime.", "info")
            else:
                self.status_var.set("Campaign complete.")
                self._append_log("Campaign complete — everyone contacted and all replies answered.", "info")
            self._update_control_states("idle")
            self._refresh_start_button()
            self._refresh_recipients_table()
            self._refresh_first_list()
        self._post(finish)

    # ---------------------------------------------------------------- start

    def _confirm_fresh_start(self, emails: list[str]) -> bool:
        ready = self._store.available_count()
        total = len(emails)
        if ready >= total:
            return messagebox.askyesno(
                "Start campaign",
                f"Send a first message to {total} recipient(s), then auto-reply to anyone who "
                f"responds?",
            )
        return messagebox.askyesno(
            "Start campaign (batched)",
            f"{total} recipients, but only {ready} message(s) are ready now.\n\n"
            f"The first {ready} will be contacted immediately. The rest are sent automatically "
            f"in later batches as the 24h message locks expire — progress is saved, so you can "
            f"close the app between batches.\n\nStart now?",
        )

    def start(self) -> None:
        if self._is_running():
            return
        emails = self._get_recipient_emails()
        errors = validate_campaign(emails, self._store)
        if errors:
            messagebox.showwarning("Start campaign", "\n".join(errors))
            return
        if not self._ensure_login() or self._client is None:
            return
        if self._inbox is None:
            messagebox.showerror(
                "IMAP required",
                "Reply detection needs IMAP. Enable it in Gmail "
                "(Settings → Forwarding and POP/IMAP → Enable IMAP), then sign in again.",
            )
            return

        resuming = self._state.has_resumable_work()
        if resuming:
            if not messagebox.askyesno(
                "Resume campaign",
                f"Resume from recipient #{self._state.cursor + 1} of {len(emails)}?",
            ):
                return
        else:
            if not self._confirm_fresh_start(emails):
                return
            self._state.begin()
            self._refresh_recipients_table()

        interval = int(self.config.get("interval_seconds", 30))
        poll = int(self.config.get("poll_interval_seconds", 60))
        self._campaign = Campaign(
            self._client, self._store, self._inbox, self._state,
            interval_seconds=interval, poll_interval_seconds=poll,
        )

        self._update_control_states("sending")
        self._update_progress(self._state.cursor, len(emails))
        self.status_var.set("Resuming campaign..." if resuming else "Starting campaign...")
        self._append_log(
            f"{'Resuming' if resuming else 'Starting'} campaign — "
            f"{self._state.cursor}/{len(emails)} already contacted.",
            "info",
        )

        if not self._campaign.start(self._campaign_callbacks()):
            self._update_control_states("idle")
            messagebox.showerror("Start campaign", "Could not start. Check inputs and try again.")

    def pause(self) -> None:
        if not self._is_running() or self._campaign.is_paused:
            return
        self._campaign.pause()
        self._update_control_states("paused")
        self.status_var.set("Paused.")
        self._append_log("Campaign paused.", "info")

    def resume(self) -> None:
        if not self._is_running() or not self._campaign.is_paused:
            return
        self._campaign.resume()
        self._update_control_states("sending")
        self.status_var.set("Resuming...")
        self._append_log("Campaign resumed.", "info")

    def stop(self) -> None:
        if not self._is_running():
            return
        self._campaign.stop()
        self.status_var.set("Stopping...")
        self._append_log("Stop requested — finishing the current step...", "info")

    def _on_close(self) -> None:
        if self._is_running():
            if not messagebox.askyesno(
                "Quit",
                "A campaign is running. Quit and resume later?\n\n"
                "Progress is saved — you can reopen the app and click Resume campaign.",
            ):
                return
            self._campaign.stop()
        self._closing = True
        if self._pump_id is not None:
            self.root.after_cancel(self._pump_id)
            self._pump_id = None
        if self._client is not None:
            self._client.close()
        if self._inbox is not None:
            self._inbox.close()
        self._save_config()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
