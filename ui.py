"""Tkinter GUI for Gmail Auto Sender."""

import csv
import json
import logging
import queue
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import paths
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
from sender import (
    MESSAGES_PER_RECIPIENT,
    EmailSender,
    Message,
    deduplicate_emails,
    validate_send_inputs,
)

CONFIG_PATH = paths.CONFIG_PATH
LOG_DIR = paths.LOG_DIR
TEMPLATES_PATH = paths.TEMPLATES_PATH

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

PUMP_INTERVAL_MS = 50

STATUS_UNREAD = "Unread"
STATUS_SENDING = "Sending"
STATUS_PARTIAL = "1 of 2"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"

STATUS_TAGS = {
    STATUS_UNREAD: "unread",
    STATUS_SENDING: "sending",
    STATUS_PARTIAL: "sending",
    STATUS_SENT: "sent",
    STATUS_FAILED: "failed",
}

THEMES = {
    "light": {
        "bg": "#f5f5f5",
        "fg": "#212529",
        "text_bg": "#ffffff",
        "text_fg": "#212529",
        "insert": "#212529",
        "select_bg": "#cce5ff",
        "disabled_bg": "#e9ecef",
    },
    "dark": {
        "bg": "#2b2b2b",
        "fg": "#e0e0e0",
        "text_bg": "#3c3c3c",
        "text_fg": "#e0e0e0",
        "insert": "#ffffff",
        "select_bg": "#4a6fa5",
        "disabled_bg": "#323232",
    },
}

DEFAULT_CONFIG = {
    "interval_seconds": 30,
    "theme": "light",
    "message1_subject": "Auto Message",
    "message1_body": "This is the first automated message.",
    "message2_subject": "Auto Message — follow up",
    "message2_body": "This is the second automated message.",
}


def create_app_icon() -> tk.PhotoImage:
    """Build a simple mail envelope icon using tkinter PhotoImage."""
    size = 64
    img = tk.PhotoImage(width=size, height=size)
    green = "#28a745"
    white = "#ffffff"

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
                (name for name in reader.fieldnames if name.strip().lower() == "email"),
                None,
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


class SignInDialog(tk.Toplevel):
    """Collect a Gmail address and app password, then verify them against Gmail."""

    def __init__(self, parent: tk.Misc, initial: Credentials | None = None):
        super().__init__(parent)
        self.title("Sign in to Gmail")
        self.geometry("440x300")
        self.resizable(False, False)
        self.transient(parent)
        self.client: GmailClient | None = None

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Gmail Account", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky=tk.W, pady=(0, 12)
        )

        ttk.Label(frame, text="Email address:").grid(row=1, column=0, sticky=tk.W, pady=(0, 4))
        self.email_var = tk.StringVar(value=initial.email if initial else "")
        email_entry = ttk.Entry(frame, textvariable=self.email_var)
        email_entry.grid(row=2, column=0, sticky=tk.EW, pady=(0, 10))

        ttk.Label(frame, text="App password:").grid(row=3, column=0, sticky=tk.W, pady=(0, 4))
        self.password_var = tk.StringVar(value=initial.app_password if initial else "")
        password_entry = ttk.Entry(frame, textvariable=self.password_var, show="•")
        password_entry.grid(row=4, column=0, sticky=tk.EW, pady=(0, 10))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Remember on this computer", variable=self.remember_var
        ).grid(row=5, column=0, sticky=tk.W, pady=(0, 8))

        ttk.Label(
            frame,
            text=(
                "Google requires a 16-character App Password for SMTP.\n"
                "Enable 2-Step Verification, then create one at\n"
                "myaccount.google.com/apppasswords"
            ),
            font=("Segoe UI", 8),
            foreground="#888",
            justify=tk.LEFT,
        ).grid(row=6, column=0, sticky=tk.W, pady=(0, 12))

        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 9)).grid(
            row=7, column=0, sticky=tk.W
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, sticky=tk.EW, pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        self.connect_btn = ttk.Button(buttons, text="Connect", command=self._connect)
        self.connect_btn.pack(side=tk.RIGHT)

        self.bind("<Return>", lambda _e: self._connect())
        email_entry.focus_set()

        self.grab_set()
        self.wait_window()

    def _connect(self) -> None:
        email = self.email_var.get().strip()
        password = normalize_app_password(self.password_var.get())

        if not email or not password:
            messagebox.showwarning("Sign in", "Email and app password are required.", parent=self)
            return

        self.status_var.set("Connecting to Gmail...")
        self.connect_btn.configure(state=tk.DISABLED)
        self.update_idletasks()

        credentials = Credentials(email=email, app_password=password)
        try:
            client = build_client(credentials)
        except (AuthenticationError, ConfigurationError) as exc:
            self.status_var.set("")
            self.connect_btn.configure(state=tk.NORMAL)
            messagebox.showerror("Sign in failed", str(exc), parent=self)
            return

        if self.remember_var.get():
            try:
                save_credentials(credentials)
            except ConfigurationError as exc:
                messagebox.showwarning("Sign in", str(exc), parent=self)
        else:
            clear_credentials()

        self.client = client
        self.destroy()


class MessageEditor(ttk.Frame):
    """Subject + body editor for one of the two messages."""

    def __init__(self, parent: tk.Misc, subject: str = "", body: str = ""):
        super().__init__(parent, padding=8)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        ttk.Label(self, text="Subject:", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 4)
        )
        self.subject_var = tk.StringVar(value=subject)
        ttk.Entry(self, textvariable=self.subject_var, font=("Segoe UI", 10)).grid(
            row=1, column=0, sticky=tk.EW, pady=(0, 8)
        )

        ttk.Label(self, text="Body:", style="Section.TLabel").grid(row=2, column=0, sticky=tk.NW)
        self.body_text = scrolledtext.ScrolledText(
            self, height=12, wrap=tk.WORD, font=("Segoe UI", 10), relief=tk.FLAT, borderwidth=1
        )
        self.body_text.grid(row=3, column=0, sticky=tk.NSEW, pady=(4, 0))
        self.body_text.insert("1.0", body)

    def get_message(self) -> Message:
        return Message(
            subject=self.subject_var.get().strip(),
            body=self.body_text.get("1.0", tk.END).strip(),
        )

    def set_message(self, subject: str, body: str) -> None:
        self.subject_var.set(subject)
        self.body_text.delete("1.0", tk.END)
        self.body_text.insert("1.0", body)


class GmailAutoSenderApp:
    """Main application window."""

    def __init__(self, root: tk.Misc | None = None):
        # Tests inject a Toplevel so the whole suite shares one Tcl interpreter;
        # repeatedly creating and destroying Tk() roots is unreliable on Windows.
        self.root = root if root is not None else tk.Tk()
        self.root.title("Gmail Auto Sender")
        self.root.geometry("1000x780")
        self.root.minsize(880, 660)

        self.config = self._load_config()
        self._setup_logging()
        self._setup_styles()
        self._setup_icon()

        self._client: GmailClient | None = None
        self._email_sender: EmailSender | None = None
        self._send_total = 0
        self._send_completed = 0
        self._templates: dict[str, dict] = {}
        self._last_messages: list[Message] = []
        self._recipient_records: dict[str, dict] = {}

        # Tk is not thread-safe. The sender thread hands work to this queue and
        # the Tk thread runs it; calling root.after() from the worker crashes on
        # a non-threaded Tcl build.
        self._ui_queue: queue.Queue = queue.Queue()
        self._pump_id: str | None = None
        self._closing = False

        self._build_ui()
        self._refresh_template_dropdown()
        self._apply_theme(self.config.get("theme", "light"))
        self._update_control_states("idle")
        self._pump()
        self._restore_saved_session()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ ui thread

    def _post(self, callback) -> None:
        """Queue a callback to run on the Tk thread. Safe from any thread."""
        self._ui_queue.put(callback)

    def _pump(self) -> None:
        """Drain queued callbacks, then reschedule. Runs only on the Tk thread."""
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
                return  # window went away mid-drain
        if not self._closing:
            self._pump_id = self.root.after(PUMP_INTERVAL_MS, self._pump)

    # ---------------------------------------------------------------- config

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
            "TFrame",
            "TLabel",
            "Title.TLabel",
            "Section.TLabel",
            "Status.TLabel",
            "Progress.TLabel",
            "TLabelframe",
            "TLabelframe.Label",
            "TNotebook",
        ):
            style.configure(name, background=theme["bg"], foreground=theme["fg"])
        style.configure("TEntry", fieldbackground=theme["text_bg"], foreground=theme["text_fg"])
        style.configure("TCombobox", fieldbackground=theme["text_bg"], foreground=theme["text_fg"])
        style.configure(
            "Treeview",
            background=theme["text_bg"],
            foreground=theme["text_fg"],
            fieldbackground=theme["text_bg"],
        )
        style.configure("Treeview.Heading", background=theme["bg"], foreground=theme["fg"])

        self._paste_colors = (theme["text_bg"], theme["disabled_bg"])
        text_widgets = [self.log_text, self.paste_text]
        text_widgets += [editor.body_text for editor in self._editors]
        for widget in text_widgets:
            widget.configure(
                bg=theme["text_bg"],
                fg=theme["text_fg"],
                insertbackground=theme["insert"],
                selectbackground=theme["select_bg"],
            )

        self.recipients_tree.tag_configure("unread", foreground=theme["fg"])
        self.recipients_tree.tag_configure("sending", foreground="#007bff")
        self.recipients_tree.tag_configure("sent", foreground="#28a745")
        self.recipients_tree.tag_configure("failed", foreground="#dc3545")

        self.theme_btn.configure(text="Light Mode" if theme_name == "dark" else "Dark Mode")

    def _toggle_theme(self) -> None:
        next_theme = "dark" if self.config.get("theme", "light") == "light" else "light"
        self._apply_theme(next_theme)
        self._save_config()

    # ------------------------------------------------------------------ auth

    def _restore_saved_session(self) -> None:
        """Reconnect with saved credentials, without blocking startup on failure."""
        credentials = load_credentials()
        if credentials is None:
            self._append_log("No saved credentials. Click Sign in to connect.", "info")
            self.status_var.set("Not signed in")
            return

        self.status_var.set(f"Reconnecting as {credentials.email}...")
        self.root.update_idletasks()
        try:
            self._set_client(build_client(credentials))
        except (AuthenticationError, ConfigurationError) as exc:
            self.status_var.set("Not signed in")
            self._append_log(f"Saved credentials rejected: {exc}", "fail")

    def _set_client(self, client: GmailClient) -> None:
        self._client = client
        interval = int(self.config.get("interval_seconds", 30))
        self._email_sender = EmailSender(client, interval_seconds=interval)
        self.account_var.set(f"Signed in: {client.email}")
        self.status_var.set(f"Connected as {client.email}")
        self._append_log(f"Signed in as {client.email}.", "success")

    def _sign_in(self) -> bool:
        if self._email_sender and self._email_sender.is_running:
            messagebox.showwarning("Sign in", "Stop sending before changing accounts.")
            return False

        dialog = SignInDialog(self.root, initial=load_credentials())
        if dialog.client is None:
            return False

        if self._client is not None:
            self._client.close()
        self._set_client(dialog.client)
        return True

    def _sign_out(self) -> None:
        if self._email_sender and self._email_sender.is_running:
            messagebox.showwarning("Sign out", "Stop sending before signing out.")
            return
        if self._client is not None:
            self._client.close()
        self._client = None
        self._email_sender = None
        clear_credentials()
        self.account_var.set("Not signed in")
        self.status_var.set("Signed out")
        self._append_log("Signed out and removed saved credentials.", "info")

    def _ensure_login(self) -> bool:
        """Return True if a usable client exists, prompting for sign-in if not."""
        if self._client is not None and self._client.is_logged_in():
            return True
        return self._sign_in()

    # -------------------------------------------------------------- settings

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.geometry("380x210")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Send interval (seconds):", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 8)
        )

        interval_var = tk.IntVar(value=int(self.config.get("interval_seconds", 30)))
        ttk.Spinbox(frame, from_=5, to=3600, increment=5, textvariable=interval_var, width=10).grid(
            row=1, column=0, sticky=tk.W, pady=(0, 12)
        )

        ttk.Label(
            frame,
            text=(
                "Waits this long after every message:\n"
                "message 1 → wait → message 2 → wait → next recipient."
            ),
            style="Progress.TLabel",
            justify=tk.LEFT,
        ).grid(row=2, column=0, sticky=tk.W, pady=(0, 16))

        def save_settings() -> None:
            try:
                interval = int(interval_var.get())
            except tk.TclError:
                messagebox.showwarning("Settings", "Enter a valid interval.", parent=dialog)
                return

            if not 5 <= interval <= 3600:
                messagebox.showwarning(
                    "Settings", "Interval must be between 5 and 3600 seconds.", parent=dialog
                )
                return

            self.config["interval_seconds"] = interval
            if self._email_sender is not None:
                self._email_sender.interval_seconds = interval
            self.interval_label_var.set(f"Interval: {interval}s after each message")
            self._save_config()
            self._append_log(f"Send interval updated to {interval}s.", "info")
            dialog.destroy()

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, sticky=tk.EW)
        ttk.Button(btn_row, text="Save", command=save_settings).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

        dialog.wait_window()

    # -------------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header_bar = ttk.Frame(container)
        header_bar.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(header_bar, text="Gmail Auto Sender", style="Title.TLabel").pack(side=tk.LEFT)

        self.account_var = tk.StringVar(value="Not signed in")
        ttk.Label(header_bar, textvariable=self.account_var, style="Status.TLabel").pack(
            side=tk.LEFT, padx=(16, 0)
        )

        toolbar = ttk.Frame(header_bar)
        toolbar.pack(side=tk.RIGHT)
        self.theme_btn = ttk.Button(toolbar, text="Dark Mode", command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(toolbar, text="Settings", command=self._open_settings).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Sign out", command=self._sign_out).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(toolbar, text="Sign in", command=self._sign_in).pack(side=tk.RIGHT, padx=(6, 0))

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        # Left panel — the two messages
        message_panel = ttk.LabelFrame(
            content, text="Messages (both are sent to every recipient)", padding=10
        )
        message_panel.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8))
        message_panel.columnconfigure(0, weight=1)
        message_panel.rowconfigure(1, weight=1)

        template_bar = ttk.Frame(message_panel)
        template_bar.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        template_bar.columnconfigure(1, weight=1)

        ttk.Label(template_bar, text="Template:", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6)
        )
        self.template_var = tk.StringVar()
        self.template_combo = ttk.Combobox(
            template_bar, textvariable=self.template_var, state="readonly", font=("Segoe UI", 10)
        )
        self.template_combo.grid(row=0, column=1, sticky=tk.EW, padx=(0, 6))
        ttk.Button(template_bar, text="Load", command=self._load_template).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(template_bar, text="Save", command=self._save_template).grid(row=0, column=3)

        notebook = ttk.Notebook(message_panel)
        notebook.grid(row=1, column=0, sticky=tk.NSEW)

        self._editors: list[MessageEditor] = []
        for index in (1, 2):
            editor = MessageEditor(
                notebook,
                subject=self.config.get(f"message{index}_subject", ""),
                body=self.config.get(f"message{index}_body", ""),
            )
            notebook.add(editor, text=f"Message {index}")
            self._editors.append(editor)

        # Right panel — recipients
        recipients_panel = ttk.LabelFrame(content, text="Recipients", padding=10)
        recipients_panel.grid(row=0, column=1, sticky=tk.NSEW, padx=(8, 0))
        recipients_panel.columnconfigure(0, weight=1)
        recipients_panel.rowconfigure(2, weight=1)

        ttk.Label(
            recipients_panel,
            text="Paste emails below (one per line), then click Import",
            style="Section.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))

        import_bar = ttk.Frame(recipients_panel)
        import_bar.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        import_bar.columnconfigure(0, weight=1)

        self.paste_text = scrolledtext.ScrolledText(
            import_bar, height=4, wrap=tk.NONE, font=("Consolas", 9), relief=tk.FLAT, borderwidth=1
        )
        self.paste_text.grid(row=0, column=0, sticky=tk.EW, columnspan=4, pady=(0, 4))

        self.import_btn = ttk.Button(
            import_bar, text="Import", command=self._import_recipients_from_paste
        )
        self.import_btn.grid(row=1, column=0, sticky=tk.W, padx=(0, 4))
        self.csv_btn = ttk.Button(import_bar, text="Load CSV", command=self._load_csv)
        self.csv_btn.grid(row=1, column=1, sticky=tk.W, padx=(0, 4))
        self.delete_btn = ttk.Button(
            import_bar, text="Delete Selected", command=self._delete_selected_recipients
        )
        self.delete_btn.grid(row=1, column=2, sticky=tk.W, padx=(0, 4))
        self.clear_btn = ttk.Button(import_bar, text="Clear All", command=self._clear_recipients)
        self.clear_btn.grid(row=1, column=3, sticky=tk.W)

        tree_frame = ttk.Frame(recipients_panel)
        tree_frame.grid(row=2, column=0, sticky=tk.NSEW)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("sent", "num", "email", "status")
        self.recipients_tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended", height=14
        )
        self.recipients_tree.bind("<Delete>", lambda _e: self._delete_selected_recipients())
        for column, heading, width, anchor, stretch in (
            ("sent", "Sent", 44, tk.CENTER, False),
            ("num", "#", 36, tk.CENTER, False),
            ("email", "Email", 220, tk.W, True),
            ("status", "Status", 80, tk.CENTER, False),
        ):
            self.recipients_tree.heading(column, text=heading)
            self.recipients_tree.column(column, width=width, anchor=anchor, stretch=stretch)

        tree_scroll = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.recipients_tree.yview
        )
        self.recipients_tree.configure(yscrollcommand=tree_scroll.set)
        self.recipients_tree.grid(row=0, column=0, sticky=tk.NSEW)
        tree_scroll.grid(row=0, column=1, sticky=tk.NS)

        self.recipient_stats_var = tk.StringVar(value="Total: 0 | Sent: 0 | Unread: 0 | Failed: 0")
        ttk.Label(
            recipients_panel, textvariable=self.recipient_stats_var, style="Progress.TLabel"
        ).grid(row=3, column=0, sticky=tk.W, pady=(6, 0))

        ttk.Label(
            recipients_panel,
            text="Tip: Ctrl+click or Shift+click to select multiple rows",
            style="Progress.TLabel",
        ).grid(row=4, column=0, sticky=tk.W, pady=(4, 0))

        # Controls
        controls = ttk.Frame(container)
        controls.pack(fill=tk.X, pady=12)

        btn_frame = ttk.Frame(controls)
        btn_frame.pack(side=tk.LEFT)

        def action_button(text, command, bg, active_bg, fg="white", active_fg="white"):
            return tk.Button(
                btn_frame,
                text=text,
                command=command,
                bg=bg,
                fg=fg,
                activebackground=active_bg,
                activeforeground=active_fg,
                font=("Segoe UI", 10, "bold"),
                relief=tk.FLAT,
                padx=20,
                pady=8,
                cursor="hand2",
            )

        self.send_btn = action_button("Send", self.start, "#28a745", "#218838")
        self.send_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.stop_btn = action_button("Stop", self.stop, "#dc3545", "#c82333")
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.pause_btn = action_button(
            "Pause", self.pause, "#ffc107", "#e0a800", fg="#212529", active_fg="#212529"
        )
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.resume_btn = action_button("Resume", self.resume, "#007bff", "#0069d9")
        self.resume_btn.pack(side=tk.LEFT)

        self.interval_label_var = tk.StringVar(
            value=f"Interval: {self.config.get('interval_seconds', 30)}s after each message"
        )
        ttk.Label(controls, textvariable=self.interval_label_var, style="Progress.TLabel").pack(
            side=tk.RIGHT
        )

        # Progress
        progress_frame = ttk.Frame(container)
        progress_frame.pack(fill=tk.X, pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100, mode="determinate"
        ).grid(row=0, column=0, sticky=tk.EW)

        self.progress_label_var = tk.StringVar(value="0/0 messages")
        ttk.Label(progress_frame, textvariable=self.progress_label_var, style="Progress.TLabel").grid(
            row=0, column=1, padx=(10, 0)
        )

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(container, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor=tk.W, pady=(0, 8)
        )

        # Live log
        log_panel = ttk.LabelFrame(container, text="Live Log", padding=10)
        log_panel.pack(fill=tk.BOTH, expand=True)
        log_panel.columnconfigure(0, weight=1)
        log_panel.rowconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_panel,
            height=9,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
            relief=tk.FLAT,
            borderwidth=1,
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        self.log_text.tag_configure("success", foreground="#28a745")
        self.log_text.tag_configure("fail", foreground="#dc3545")
        self.log_text.tag_configure("info", foreground="#555555")

    # ------------------------------------------------------------- templates

    def _load_templates(self) -> dict[str, dict]:
        if not TEMPLATES_PATH.exists():
            return {}
        try:
            with TEMPLATES_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            messagebox.showerror("Templates", "Could not read templates.json.")
        return {}

    def _write_templates(self, templates: dict[str, dict]) -> bool:
        try:
            with TEMPLATES_PATH.open("w", encoding="utf-8") as f:
                json.dump(templates, f, indent=2, ensure_ascii=False)
            return True
        except OSError as exc:
            messagebox.showerror("Templates", f"Could not save templates:\n{exc}")
            return False

    def _refresh_template_dropdown(self) -> None:
        self._templates = self._load_templates()
        names = sorted(self._templates.keys())
        self.template_combo["values"] = names
        if names and self.template_var.get() not in names:
            self.template_var.set(names[0])
        elif not names:
            self.template_var.set("")

    def _save_template(self) -> None:
        messages = [editor.get_message() for editor in self._editors]
        if all(m.is_empty() for m in messages):
            messagebox.showwarning("Save Template", "Both messages are empty.")
            return

        name = simpledialog.askstring("Save Template", "Template name:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()

        self._templates[name] = {
            f"message{i}": {"subject": m.subject, "body": m.body}
            for i, m in enumerate(messages, start=1)
        }
        if self._write_templates(self._templates):
            self._refresh_template_dropdown()
            self.template_var.set(name)
            self._append_log(f'Template saved: "{name}"', "info")

    def _load_template(self) -> None:
        name = self.template_var.get().strip()
        if not name:
            messagebox.showwarning("Load Template", "Select a template from the dropdown.")
            return

        template = self._templates.get(name)
        if template is None:
            self._refresh_template_dropdown()
            messagebox.showwarning("Load Template", f'Template "{name}" was not found.')
            return

        # Templates written before two-message support hold a single subject/body pair.
        if "subject" in template or "body" in template:
            legacy = {"subject": template.get("subject", ""), "body": template.get("body", "")}
            template = {"message1": legacy, "message2": legacy}

        for index, editor in enumerate(self._editors, start=1):
            part = template.get(f"message{index}", {})
            editor.set_message(part.get("subject", ""), part.get("body", ""))

        self._append_log(f'Template loaded: "{name}"', "info")

    # ------------------------------------------------------------ recipients

    def _extract_emails(self, text: str) -> list[str]:
        """Pull valid addresses out of pasted text (one per line, or comma/semicolon separated)."""
        emails = []
        for line in text.splitlines():
            for candidate in re.split(r"[,;\s]+", line):
                candidate = candidate.strip().strip("\"'<>")
                if candidate and EMAIL_PATTERN.match(candidate):
                    emails.append(candidate)
        return emails

    def _recipient_key(self, email: str) -> str:
        return email.strip().lower()

    def _is_sending(self) -> bool:
        return self._email_sender is not None and self._email_sender.is_running

    def _clear_recipients(self) -> None:
        if self._is_sending():
            messagebox.showwarning("Recipients", "Stop sending before clearing the list.")
            return
        if not self._recipient_records:
            return
        if not messagebox.askyesno("Clear All", "Remove all recipients from the list?"):
            return
        for item in self.recipients_tree.get_children():
            self.recipients_tree.delete(item)
        self._recipient_records.clear()
        self._refresh_recipient_stats()
        self._append_log("Cleared all recipients.", "info")

    def _renumber_recipients(self) -> None:
        for num, key in enumerate(self.recipients_tree.get_children(), start=1):
            record = self._recipient_records.get(key)
            if record:
                self._write_row(key, record, num)

    def _write_row(self, key: str, record: dict, num: int) -> None:
        check = "☑" if record["sent"] else "☐"
        tag = STATUS_TAGS.get(record["status"], "unread")
        self.recipients_tree.item(
            key, values=(check, num, record["email"], record["status"]), tags=(tag,)
        )

    def _delete_selected_recipients(self) -> None:
        if self._is_sending():
            messagebox.showwarning("Delete", "Stop sending before deleting recipients.")
            return

        selected = self.recipients_tree.selection()
        if not selected:
            messagebox.showwarning(
                "Delete",
                "Select one or more recipients to delete.\n\n"
                "Use Ctrl+click to select multiple, or Shift+click for a range.",
            )
            return

        count = len(selected)
        if count == 1:
            record = self._recipient_records.get(selected[0])
            prompt = f"Delete {record['email'] if record else selected[0]}?"
        else:
            prompt = f"Delete {count} selected recipients?"

        if not messagebox.askyesno("Delete Recipients", prompt):
            return

        for key in selected:
            self.recipients_tree.delete(key)
            self._recipient_records.pop(key, None)

        self._renumber_recipients()
        self._refresh_recipient_stats()
        self._append_log(f"Deleted {count} recipient(s).", "info")

    def _add_recipients(self, emails: list[str]) -> int:
        """Add emails to the recipient list. Returns the number newly added."""
        unique, duplicates_removed = deduplicate_emails(emails)
        added = 0

        for email in unique:
            key = self._recipient_key(email)
            if key in self._recipient_records:
                continue

            self._recipient_records[key] = {
                "email": email,
                "sent": False,
                "status": STATUS_UNREAD,
            }
            self.recipients_tree.insert("", tk.END, iid=key)
            added += 1

        self._renumber_recipients()
        self._refresh_recipient_stats()

        if duplicates_removed:
            self._append_log(f"Skipped {duplicates_removed} duplicate address(es).", "info")

        return added

    def _get_recipient_emails(self) -> list[str]:
        emails = []
        for key in self.recipients_tree.get_children():
            record = self._recipient_records.get(key)
            if record:
                emails.append(record["email"])
        return emails

    def _set_recipient_row(self, email: str, status: str, sent: bool | None = None) -> None:
        key = self._recipient_key(email)
        record = self._recipient_records.get(key)
        if not record:
            return

        if sent is not None:
            record["sent"] = sent
        record["status"] = status

        children = self.recipients_tree.get_children()
        num = children.index(key) + 1 if key in children else 0
        self._write_row(key, record, num)

    def _reset_recipients_for_send(self, emails: list[str]) -> None:
        for email in emails:
            self._set_recipient_row(email, STATUS_UNREAD, sent=False)

    def _refresh_recipient_stats(self) -> None:
        records = self._recipient_records.values()
        total = len(self._recipient_records)
        sent = sum(1 for r in records if r["sent"])
        unread = sum(1 for r in records if r["status"] == STATUS_UNREAD)
        failed = sum(1 for r in records if r["status"] == STATUS_FAILED)
        self.recipient_stats_var.set(
            f"Total: {total} | Sent: {sent} | Unread: {unread} | Failed: {failed}"
        )

    def _import_recipients_from_paste(self) -> None:
        if self._is_sending():
            messagebox.showwarning("Import", "Stop sending before importing recipients.")
            return

        text = self.paste_text.get("1.0", tk.END)
        raw = self._extract_emails(text)
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
        if self._is_sending():
            messagebox.showwarning("Load CSV", "Stop sending before importing recipients.")
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

    # -------------------------------------------------------------- controls

    def _update_control_states(self, state: str) -> None:
        """Drive every stateful widget from one place: idle, sending, or paused."""
        sending = state == "sending"
        paused = state == "paused"
        idle = state == "idle"

        self.send_btn.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self.stop_btn.configure(state=tk.DISABLED if idle else tk.NORMAL)
        self.pause_btn.configure(state=tk.NORMAL if sending else tk.DISABLED)
        self.resume_btn.configure(state=tk.NORMAL if paused else tk.DISABLED)

        for button in (self.import_btn, self.csv_btn, self.delete_btn, self.clear_btn):
            button.configure(state=tk.NORMAL if idle else tk.DISABLED)

        # The paste box is the one widget that must never be left disabled — a
        # send that ends any way other than "idle" used to strand it read-only.
        normal_bg, disabled_bg = getattr(self, "_paste_colors", ("#ffffff", "#e9ecef"))
        self.paste_text.configure(
            state=tk.NORMAL if idle else tk.DISABLED,
            bg=normal_bg if idle else disabled_bg,
        )

    def _update_progress(self, current: int, total: int) -> None:
        self.progress_label_var.set(f"{current}/{total} messages")
        self.progress_var.set((current / total) * 100 if total else 0)

    # ------------------------------------------------------------- callbacks

    def _on_send_status(self, email: str, message_index: int) -> None:
        def update() -> None:
            self.status_var.set(f"Sending message {message_index} to {email}...")
            self._set_recipient_row(email, STATUS_SENDING)

        self._post(update)

    def _on_send_result(self, result: SendResult, current: int, total: int) -> None:
        def update() -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            if result.success:
                self._append_log(
                    f"✅ Message {result.message_index} sent to {result.email} — {timestamp}",
                    "success",
                )
            else:
                self._append_log(
                    f"❌ Message {result.message_index} failed for {result.email}"
                    f" — {timestamp} ({result.error})",
                    "fail",
                )
                if result.message_index < MESSAGES_PER_RECIPIENT:
                    self._append_log(
                        f"   Skipping message {result.message_index + 1} for {result.email}.",
                        "info",
                    )

            self._send_completed = current
            self._update_progress(current, total)

            if not result.success:
                self._set_recipient_row(result.email, STATUS_FAILED, sent=False)
            elif self._email_sender and self._email_sender.pending_messages(result.email):
                self._set_recipient_row(result.email, STATUS_PARTIAL, sent=False)
            else:
                self._set_recipient_row(result.email, STATUS_SENT, sent=True)

            self._refresh_recipient_stats()

            key = self._recipient_key(result.email)
            if key in self.recipients_tree.get_children():
                self.recipients_tree.see(key)

        self._post(update)

    def _on_send_complete(
        self, stopped: bool, _results: list[SendResult], failed_emails: list[str]
    ) -> None:
        def finish() -> None:
            retry_started = False
            try:
                if stopped:
                    for record in self._recipient_records.values():
                        if record["status"] in (STATUS_SENDING, STATUS_PARTIAL):
                            self._set_recipient_row(
                                record["email"], STATUS_UNREAD, sent=record["sent"]
                            )
                    self._refresh_recipient_stats()
                    self.status_var.set("Stopped.")
                    self._append_log("Sending stopped by user.", "info")
                    return

                self._update_progress(self._send_total, self._send_total)
                self.status_var.set("Finished sending.")
                self._append_log("Sending complete.", "info")

                if failed_emails:
                    count = len(failed_emails)
                    if messagebox.askyesno(
                        "Retry Failed Emails",
                        f"{count} recipient{'s' if count != 1 else ''} did not receive every "
                        f"message. Retry only the undelivered messages?",
                    ):
                        retry_started = self._start_retry(failed_emails)
                        return
            finally:
                # Every path that is not handing off to a retry must give the
                # controls — above all the paste box — back to the user. Keyed on
                # an explicit flag, not on is_running: on_complete fires from
                # inside the worker thread, which is still alive at this point.
                if not retry_started:
                    self._update_control_states("idle")

        self._post(finish)

    # ---------------------------------------------------------------- sending

    def _begin_send_ui(self, total_messages: int, status: str) -> None:
        self._update_control_states("sending")
        self._send_total = total_messages
        self._send_completed = 0
        self._update_progress(0, total_messages)
        self.status_var.set(status)

    def _start_retry(self, failed_emails: list[str]) -> bool:
        """Resend the undelivered messages. Returns True only if a send actually began."""
        if not self._ensure_login() or self._email_sender is None:
            return False

        pending = sum(len(self._email_sender.pending_messages(e)) for e in failed_emails)
        self._begin_send_ui(pending, "Retrying undelivered messages...")
        self._append_log(f"Retrying {pending} undelivered message(s)...", "info")

        for email in failed_emails:
            self._set_recipient_row(email, STATUS_UNREAD, sent=False)

        if not self._email_sender.start_retry(
            emails=failed_emails,
            messages=self._last_messages,
            on_status=self._on_send_status,
            on_result=self._on_send_result,
            on_complete=self._on_send_complete,
        ):
            messagebox.showerror("Retry", "Could not start the retry.")
            return False

        return True

    def start(self) -> None:
        if self._is_sending():
            return

        emails = self._get_recipient_emails()
        messages = [editor.get_message() for editor in self._editors]

        errors = validate_send_inputs(emails, messages)
        if errors:
            messagebox.showwarning("Send", "\n".join(errors))
            return

        interval = int(self.config.get("interval_seconds", 30))
        total_messages = len(emails) * MESSAGES_PER_RECIPIENT
        if not messagebox.askyesno(
            "Confirm Send",
            f"Send {MESSAGES_PER_RECIPIENT} messages to each of {len(emails)} recipient(s)?\n\n"
            f"That is {total_messages} emails, {interval}s apart.",
        ):
            return

        if not self._ensure_login() or self._email_sender is None:
            return

        self._last_messages = messages
        self._reset_recipients_for_send(emails)

        self._email_sender.interval_seconds = interval
        self.interval_label_var.set(f"Interval: {interval}s after each message")

        self._begin_send_ui(total_messages, "Starting...")
        self._append_log(
            f"Starting send: {MESSAGES_PER_RECIPIENT} messages × {len(emails)} recipient(s).",
            "info",
        )

        if not self._email_sender.start(
            emails=emails,
            messages=messages,
            on_status=self._on_send_status,
            on_result=self._on_send_result,
            on_complete=self._on_send_complete,
        ):
            self._update_control_states("idle")
            messagebox.showerror("Send", "Could not start sending. Check inputs and try again.")

    def pause(self) -> None:
        if not self._is_sending() or self._email_sender.is_paused:
            return
        self._email_sender.pause()
        self._update_control_states("paused")
        self.status_var.set(f"Paused at {self._send_completed}/{self._send_total}")
        self._append_log(f"Paused at {self._send_completed}/{self._send_total}.", "info")

    def resume(self) -> None:
        if not self._is_sending() or not self._email_sender.is_paused:
            return
        self._email_sender.resume()
        self._update_control_states("sending")
        self.status_var.set("Resuming...")
        self._append_log("Sending resumed.", "info")

    def stop(self) -> None:
        if not self._is_sending():
            return
        self._email_sender.stop()
        self.status_var.set("Stopping...")
        self._append_log("Stop requested — finishing the current message...", "info")

    def _on_close(self) -> None:
        if self._is_sending():
            if not messagebox.askyesno("Quit", "A send is in progress. Stop it and quit?"):
                return
            self._email_sender.stop()

        self._closing = True
        if self._pump_id is not None:
            self.root.after_cancel(self._pump_id)
            self._pump_id = None

        if self._client is not None:
            self._client.close()
        self._persist_messages()
        self.root.destroy()

    def _persist_messages(self) -> None:
        for index, editor in enumerate(self._editors, start=1):
            message = editor.get_message()
            self.config[f"message{index}_subject"] = message.subject
            self.config[f"message{index}_body"] = message.body
        self._save_config()

    def run(self) -> None:
        self.root.mainloop()
