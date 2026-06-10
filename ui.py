"""Tkinter GUI for Gmail Auto Sender."""

import csv
import json
import logging
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from auth import get_gmail_service, is_logged_in, sign_out
from sender import EmailSender, SendResult, deduplicate_emails, validate_send_inputs

CONFIG_PATH = Path("config.json")
LOG_DIR = Path("logs")
TEMPLATES_PATH = Path("templates.json")

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

STATUS_UNREAD = "Unread"
STATUS_SENDING = "Sending"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"

THEMES = {
    "light": {
        "bg": "#f5f5f5",
        "fg": "#212529",
        "text_bg": "#ffffff",
        "text_fg": "#212529",
        "insert": "#212529",
        "select_bg": "#cce5ff",
    },
    "dark": {
        "bg": "#2b2b2b",
        "fg": "#e0e0e0",
        "text_bg": "#3c3c3c",
        "text_fg": "#e0e0e0",
        "insert": "#ffffff",
        "select_bg": "#4a6fa5",
    },
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
            if in_body or in_flap or in_seal:
                row.append(white)
            else:
                row.append(green)
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


class GmailAutoSenderApp:
    """Main application window."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gmail Auto Sender")
        self.root.geometry("960x720")
        self.root.minsize(800, 600)
        self.root.configure(bg="#f5f5f5")

        self.config = self._load_config()
        self._setup_logging()
        self._setup_styles()
        self._setup_icon()

        interval = self.config.get("interval_seconds", 30)
        self._email_sender = EmailSender(interval_seconds=interval)
        self._send_total = 0
        self._send_current = 0
        self._send_completed = 0
        self._templates: dict[str, dict[str, str]] = {}
        self._last_subject = ""
        self._last_body = ""
        self._gmail_service = None
        self._recipient_records: dict[str, dict] = {}

        self._build_ui()
        self._refresh_template_dropdown()
        self._apply_theme(self.config.get("theme", "light"))

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open(encoding="utf-8") as f:
                return json.load(f)
        return {
            "interval_seconds": 30,
            "theme": "light",
            "default_subject": "Auto Message",
            "default_body": "This is an automated message from Gmail Auto Sender.",
        }

    def _save_config(self) -> None:
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            messagebox.showerror("Settings", f"Could not save config.json:\n{exc}")

    def _setup_icon(self) -> None:
        self._app_icon = create_app_icon()
        self.root.iconphoto(True, self._app_icon)

    def _setup_logging(self):
        LOG_DIR.mkdir(exist_ok=True)
        logging.basicConfig(
            filename=LOG_DIR / "app.log",
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    def _setup_styles(self):
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
        style.configure("TFrame", background=theme["bg"])
        style.configure("TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("Title.TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("Section.TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("Status.TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("Progress.TLabel", background=theme["bg"], foreground=theme["fg"])
        style.configure("TLabelframe", background=theme["bg"], foreground=theme["fg"])
        style.configure("TLabelframe.Label", background=theme["bg"], foreground=theme["fg"])
        style.configure("TEntry", fieldbackground=theme["text_bg"], foreground=theme["text_fg"])
        style.configure("TCombobox", fieldbackground=theme["text_bg"], foreground=theme["text_fg"])

        style.configure(
            "Treeview",
            background=theme["text_bg"],
            foreground=theme["text_fg"],
            fieldbackground=theme["text_bg"],
        )
        style.configure("Treeview.Heading", background=theme["bg"], foreground=theme["fg"])

        for widget in (self.body_text, self.paste_text, self.log_text):
            widget.configure(
                bg=theme["text_bg"],
                fg=theme["text_fg"],
                insertbackground=theme["insert"],
                selectbackground=theme["select_bg"],
            )

        if hasattr(self, "recipients_tree"):
            self.recipients_tree.tag_configure("unread", foreground=theme["fg"])
            self.recipients_tree.tag_configure("sending", foreground="#007bff")
            self.recipients_tree.tag_configure("sent", foreground="#28a745")
            self.recipients_tree.tag_configure("failed", foreground="#dc3545")

        if hasattr(self, "theme_btn"):
            label = "Light Mode" if theme_name == "dark" else "Dark Mode"
            self.theme_btn.configure(text=label)

    def _toggle_theme(self) -> None:
        next_theme = "dark" if self.config.get("theme", "light") == "light" else "light"
        self._apply_theme(next_theme)
        self._save_config()

    def _sign_in_gmail(self) -> bool:
        """Run Gmail OAuth on the main thread (required for browser login)."""
        self.status_var.set("Opening Gmail sign-in...")
        self.root.update_idletasks()
        self._append_log("Gmail sign-in started — complete login in your browser.", "info")

        try:
            self._gmail_service = get_gmail_service()
        except Exception as exc:
            self._gmail_service = None
            self.status_var.set("Gmail sign-in failed.")
            messagebox.showerror("Gmail Sign-in", str(exc))
            self._append_log(f"Gmail sign-in failed: {exc}", "fail")
            return False

        self.status_var.set("Gmail connected.")
        self._append_log("Gmail sign-in successful.", "success")
        messagebox.showinfo("Gmail Sign-in", "You are signed in. You can now send emails.")
        return True

    def _ensure_gmail_login(self) -> bool:
        """Authenticate on the main thread before starting the send worker."""
        if self._gmail_service is not None and is_logged_in():
            return True

        if is_logged_in():
            try:
                self._gmail_service = get_gmail_service()
                return True
            except Exception as exc:
                self._gmail_service = None
                messagebox.showerror("Gmail Sign-in", str(exc))
                return False

        return self._sign_in_gmail()

    def _switch_gmail_account(self) -> None:
        """Sign out and sign in again to use a different Gmail account."""
        if self._email_sender.is_running:
            messagebox.showwarning(
                "Switch Account",
                "Stop sending before switching Gmail accounts.",
            )
            return

        if not messagebox.askyesno(
            "Switch Gmail Account",
            "Sign out of the current Gmail and sign in with another account?",
        ):
            return

        sign_out()
        self._gmail_service = None
        self.status_var.set("Signed out.")
        self._append_log("Signed out of Gmail. Sign in with your other account.", "info")
        self._sign_in_gmail()

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.geometry("360x180")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Send interval (seconds):", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 8)
        )

        interval_var = tk.IntVar(value=self.config.get("interval_seconds", 30))
        interval_spin = ttk.Spinbox(
            frame,
            from_=5,
            to=600,
            increment=5,
            textvariable=interval_var,
            width=10,
        )
        interval_spin.grid(row=1, column=0, sticky=tk.W, pady=(0, 12))

        ttk.Label(
            frame,
            text="Delay between each email (default: 30s)",
            style="Progress.TLabel",
        ).grid(row=2, column=0, sticky=tk.W, pady=(0, 16))

        def save_settings():
            try:
                interval = int(interval_var.get())
            except tk.TclError:
                messagebox.showwarning("Settings", "Enter a valid interval.", parent=dialog)
                return

            if interval < 5 or interval > 600:
                messagebox.showwarning(
                    "Settings", "Interval must be between 5 and 600 seconds.", parent=dialog
                )
                return

            self.config["interval_seconds"] = interval
            self._email_sender.interval_seconds = interval
            self.interval_label_var.set(f"Interval: {interval}s between emails")
            self._save_config()
            self._append_log(f"Send interval updated to {interval}s.", "info")
            dialog.destroy()

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, sticky=tk.EW)
        ttk.Button(btn_row, text="Save", command=save_settings).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

        dialog.wait_window()

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill=tk.BOTH, expand=True)

        header_bar = ttk.Frame(container)
        header_bar.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(header_bar, text="Gmail Auto Sender", style="Title.TLabel").pack(
            side=tk.LEFT
        )

        toolbar = ttk.Frame(header_bar)
        toolbar.pack(side=tk.RIGHT)

        ttk.Button(toolbar, text="Sign in with Google", command=self._sign_in_gmail).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Switch Account", command=self._switch_gmail_account).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(toolbar, text="Settings", command=self._open_settings).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        self.theme_btn = ttk.Button(toolbar, text="Dark Mode", command=self._toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT, padx=(6, 0))

        content = ttk.Frame(container)
        content.pack(fill=tk.BOTH, expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(0, weight=1)

        # Left panel — message
        message_panel = ttk.LabelFrame(content, text="Message", padding=10)
        message_panel.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8))
        message_panel.columnconfigure(0, weight=1)
        message_panel.rowconfigure(2, weight=1)

        ttk.Label(message_panel, text="Subject:", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 4)
        )
        self.subject_var = tk.StringVar(value=self.config.get("default_subject", ""))
        subject_entry = ttk.Entry(message_panel, textvariable=self.subject_var, font=("Segoe UI", 10))
        subject_entry.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))

        template_bar = ttk.Frame(message_panel)
        template_bar.grid(row=2, column=0, sticky=tk.EW, pady=(0, 8))
        template_bar.columnconfigure(1, weight=1)

        ttk.Label(template_bar, text="Template:", style="Section.TLabel").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6)
        )
        self.template_var = tk.StringVar()
        self.template_combo = ttk.Combobox(
            template_bar,
            textvariable=self.template_var,
            state="readonly",
            font=("Segoe UI", 10),
        )
        self.template_combo.grid(row=0, column=1, sticky=tk.EW, padx=(0, 6))

        ttk.Button(template_bar, text="Load Template", command=self._load_template).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(template_bar, text="Save Template", command=self._save_template).grid(
            row=0, column=3
        )

        ttk.Label(message_panel, text="Body:", style="Section.TLabel").grid(
            row=3, column=0, sticky=tk.NW
        )
        self.body_text = scrolledtext.ScrolledText(
            message_panel, height=16, wrap=tk.WORD, font=("Segoe UI", 10), relief=tk.FLAT, borderwidth=1
        )
        self.body_text.grid(row=4, column=0, sticky=tk.NSEW, pady=(4, 0))
        self.body_text.insert(tk.END, self.config.get("default_body", ""))
        message_panel.rowconfigure(4, weight=1)

        # Right panel — recipients
        recipients_panel = ttk.LabelFrame(content, text="Recipients", padding=10)
        recipients_panel.grid(row=0, column=1, sticky=tk.NSEW, padx=(8, 0))
        recipients_panel.columnconfigure(0, weight=1)
        recipients_panel.rowconfigure(2, weight=1)

        ttk.Label(
            recipients_panel,
            text="Paste emails below, then click Import",
            style="Section.TLabel",
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 4))

        import_bar = ttk.Frame(recipients_panel)
        import_bar.grid(row=1, column=0, sticky=tk.EW, pady=(0, 6))
        import_bar.columnconfigure(0, weight=1)

        self.paste_text = scrolledtext.ScrolledText(
            import_bar, height=4, wrap=tk.NONE, font=("Consolas", 9), relief=tk.FLAT, borderwidth=1
        )
        self.paste_text.grid(row=0, column=0, sticky=tk.EW, columnspan=4, pady=(0, 4))

        ttk.Button(import_bar, text="Import", command=self._import_recipients_from_paste).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 4)
        )
        ttk.Button(import_bar, text="Load CSV", command=self._load_csv).grid(
            row=1, column=1, sticky=tk.W, padx=(0, 4)
        )
        ttk.Button(import_bar, text="Delete Selected", command=self._delete_selected_recipients).grid(
            row=1, column=2, sticky=tk.W, padx=(0, 4)
        )
        ttk.Button(import_bar, text="Clear All", command=self._clear_recipients).grid(
            row=1, column=3, sticky=tk.W
        )

        ttk.Label(
            recipients_panel,
            text="Tip: Ctrl+click or Shift+click to select multiple rows",
            style="Progress.TLabel",
        ).grid(row=4, column=0, sticky=tk.W, pady=(4, 0))

        tree_frame = ttk.Frame(recipients_panel)
        tree_frame.grid(row=2, column=0, sticky=tk.NSEW)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        columns = ("sent", "num", "email", "status")
        self.recipients_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=14,
        )
        self.recipients_tree.bind("<Delete>", lambda _event: self._delete_selected_recipients())
        self.recipients_tree.heading("sent", text="Sent")
        self.recipients_tree.heading("num", text="#")
        self.recipients_tree.heading("email", text="Email")
        self.recipients_tree.heading("status", text="Status")
        self.recipients_tree.column("sent", width=44, anchor=tk.CENTER, stretch=False)
        self.recipients_tree.column("num", width=36, anchor=tk.CENTER, stretch=False)
        self.recipients_tree.column("email", width=220, anchor=tk.W)
        self.recipients_tree.column("status", width=72, anchor=tk.CENTER, stretch=False)

        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.recipients_tree.yview)
        self.recipients_tree.configure(yscrollcommand=tree_scroll.set)
        self.recipients_tree.grid(row=0, column=0, sticky=tk.NSEW)
        tree_scroll.grid(row=0, column=1, sticky=tk.NS)

        self.recipient_stats_var = tk.StringVar(value="Total: 0 | Sent: 0 | Unread: 0 | Failed: 0")
        ttk.Label(
            recipients_panel, textvariable=self.recipient_stats_var, style="Progress.TLabel"
        ).grid(row=3, column=0, sticky=tk.W, pady=(6, 0))

        # Controls
        controls = ttk.Frame(container)
        controls.pack(fill=tk.X, pady=12)

        btn_frame = ttk.Frame(controls)
        btn_frame.pack(side=tk.LEFT)

        self.send_btn = tk.Button(
            btn_frame,
            text="Send",
            command=self.start,
            bg="#28a745",
            fg="white",
            activebackground="#218838",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=20,
            pady=8,
            cursor="hand2",
        )
        self.send_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = tk.Button(
            btn_frame,
            text="Stop",
            command=self.stop,
            bg="#dc3545",
            fg="white",
            activebackground="#c82333",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=20,
            pady=8,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.pause_btn = tk.Button(
            btn_frame,
            text="Pause",
            command=self.pause,
            bg="#ffc107",
            fg="#212529",
            activebackground="#e0a800",
            activeforeground="#212529",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=20,
            pady=8,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.resume_btn = tk.Button(
            btn_frame,
            text="Resume",
            command=self.resume,
            bg="#007bff",
            fg="white",
            activebackground="#0069d9",
            activeforeground="white",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=20,
            pady=8,
            cursor="hand2",
            state=tk.DISABLED,
        )
        self.resume_btn.pack(side=tk.LEFT)

        self.interval_label_var = tk.StringVar(
            value=f"Interval: {self.config.get('interval_seconds', 30)}s between emails"
        )
        ttk.Label(
            controls,
            textvariable=self.interval_label_var,
            style="Progress.TLabel",
        ).pack(side=tk.RIGHT)

        # Progress
        progress_frame = ttk.Frame(container)
        progress_frame.pack(fill=tk.X, pady=(0, 8))
        progress_frame.columnconfigure(0, weight=1)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.grid(row=0, column=0, sticky=tk.EW)

        self.progress_label_var = tk.StringVar(value="0/0")
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
            height=10,
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

    def _load_templates(self) -> dict[str, dict[str, str]]:
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

    def _write_templates(self, templates: dict[str, dict[str, str]]) -> bool:
        try:
            with TEMPLATES_PATH.open("w", encoding="utf-8") as f:
                json.dump(templates, f, indent=2, ensure_ascii=False)
            return True
        except OSError as exc:
            messagebox.showerror("Templates", f"Could not save templates:\n{exc}")
            return False

    def _refresh_template_dropdown(self):
        self._templates = self._load_templates()
        names = sorted(self._templates.keys())
        self.template_combo["values"] = names
        if names:
            if self.template_var.get() not in names:
                self.template_var.set(names[0])
        else:
            self.template_var.set("")

    def _save_template(self):
        subject = self.subject_var.get().strip()
        body = self.body_text.get("1.0", tk.END).strip()

        if not subject and not body:
            messagebox.showwarning("Save Template", "Subject and body are empty.")
            return

        name = simpledialog.askstring(
            "Save Template",
            "Template name:",
            parent=self.root,
        )
        if not name:
            return

        name = name.strip()
        if not name:
            messagebox.showwarning("Save Template", "Template name cannot be empty.")
            return

        self._templates[name] = {"subject": subject, "body": body}
        if self._write_templates(self._templates):
            self._refresh_template_dropdown()
            self.template_var.set(name)
            messagebox.showinfo("Save Template", f'Template "{name}" saved.')
            self._append_log(f'Template saved: "{name}"', "info")

    def _load_template(self):
        name = self.template_var.get().strip()
        if not name:
            messagebox.showwarning("Load Template", "Select a template from the dropdown.")
            return

        template = self._templates.get(name)
        if template is None:
            self._refresh_template_dropdown()
            messagebox.showwarning("Load Template", f'Template "{name}" was not found.')
            return

        self.subject_var.set(template.get("subject", ""))
        self.body_text.delete("1.0", tk.END)
        self.body_text.insert(tk.END, template.get("body", ""))
        self._append_log(f'Template loaded: "{name}"', "info")

    def _extract_emails(self, text: str) -> list[str]:
        """Extract valid email addresses from paste text."""
        emails = []
        for line in text.splitlines():
            candidate = line.strip().strip('"').strip("'")
            if candidate and EMAIL_PATTERN.match(candidate):
                emails.append(candidate)
        return emails

    def _recipient_key(self, email: str) -> str:
        return email.strip().lower()

    def _clear_recipients(self) -> None:
        if self._email_sender.is_running:
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
        """Refresh row numbers after add/delete."""
        for num, key in enumerate(self.recipients_tree.get_children(), start=1):
            record = self._recipient_records.get(key)
            if not record:
                continue
            check = "☑" if record["sent"] else "☐"
            tag = record["status"].lower()
            self.recipients_tree.item(
                key,
                values=(check, num, record["email"], record["status"]),
                tags=(tag,),
            )

    def _delete_selected_recipients(self) -> None:
        """Delete one or more selected recipients from the list."""
        if self._email_sender.is_running:
            messagebox.showwarning(
                "Delete",
                "Stop sending before deleting recipients.",
            )
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
            label = record["email"] if record else selected[0]
            prompt = f"Delete {label}?"
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

    def _add_recipients(self, emails: list[str], replace: bool = False) -> int:
        """Add emails to the recipient list. Returns number newly added."""
        if replace:
            self._clear_recipients()

        unique, duplicates_removed = deduplicate_emails(emails)
        added = 0

        for email in unique:
            key = self._recipient_key(email)
            if key in self._recipient_records:
                continue

            num = len(self.recipients_tree.get_children()) + 1
            self.recipients_tree.insert(
                "",
                tk.END,
                iid=key,
                values=("☐", num, email, STATUS_UNREAD),
                tags=("unread",),
            )
            self._recipient_records[key] = {
                "email": email,
                "sent": False,
                "status": STATUS_UNREAD,
            }
            added += 1

        self._renumber_recipients()
        self._refresh_recipient_stats()

        if duplicates_removed:
            self._append_log(
                f"Skipped {duplicates_removed} duplicate address(es).", "info"
            )

        return added

    def _get_recipient_emails(self) -> list[str]:
        """Return recipient emails in list order."""
        emails = []
        for key in self.recipients_tree.get_children():
            record = self._recipient_records.get(key)
            if record:
                emails.append(record["email"])
        return emails

    def _set_recipient_row(
        self, email: str, status: str, sent: bool | None = None
    ) -> None:
        key = self._recipient_key(email)
        record = self._recipient_records.get(key)
        if not record:
            return

        if sent is not None:
            record["sent"] = sent
        record["status"] = status

        children = self.recipients_tree.get_children()
        num = children.index(key) + 1 if key in children else "?"
        check = "☑" if record["sent"] else "☐"
        tag = status.lower()

        self.recipients_tree.item(
            key,
            values=(check, num, record["email"], status),
            tags=(tag,),
        )

    def _reset_recipients_for_send(self, emails: list[str]) -> None:
        """Mark recipients in the send batch as Unread before sending."""
        for email in emails:
            self._set_recipient_row(email, STATUS_UNREAD, sent=False)

    def _refresh_recipient_stats(self) -> None:
        total = len(self._recipient_records)
        sent = sum(1 for r in self._recipient_records.values() if r["sent"])
        unread = sum(
            1 for r in self._recipient_records.values() if r["status"] == STATUS_UNREAD
        )
        failed = sum(
            1 for r in self._recipient_records.values() if r["status"] == STATUS_FAILED
        )
        self.recipient_stats_var.set(
            f"Total: {total} | Sent: {sent} | Unread: {unread} | Failed: {failed}"
        )

    def _import_recipients_from_paste(self) -> None:
        text = self.paste_text.get("1.0", tk.END)
        raw = self._extract_emails(text)
        if not raw:
            messagebox.showwarning("Import", "No valid email addresses found.")
            return

        added = self._add_recipients(raw)
        if added:
            self.paste_text.delete("1.0", tk.END)
            self._append_log(f"Imported {added} recipient(s).", "info")
        else:
            messagebox.showinfo("Import", "All addresses are already in the list.")

    def _load_csv(self):
        emails = load_csv_emails(self.root)
        if emails is None:
            return
        if emails:
            added = self._add_recipients(emails)
            if added:
                self._append_log(f"Added {added} recipient(s) from CSV.", "info")

    def _session_log_path(self) -> Path:
        return LOG_DIR / f"session_{datetime.now().strftime('%Y-%m-%d')}.log"

    def _write_session_log(self, message: str) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        with self._session_log_path().open("a", encoding="utf-8") as f:
            f.write(message + "\n")

    def _append_log(self, message: str, tag: str = "info"):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self._write_session_log(message)

    def _update_control_states(self, state: str):
        """Update button states: idle, sending, paused."""
        if state == "idle":
            self.send_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.pause_btn.configure(state=tk.DISABLED)
            self.resume_btn.configure(state=tk.DISABLED)
            self.paste_text.configure(state=tk.NORMAL)
        elif state == "sending":
            self.send_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
            self.pause_btn.configure(state=tk.NORMAL)
            self.resume_btn.configure(state=tk.DISABLED)
            self.paste_text.configure(state=tk.DISABLED)
        elif state == "paused":
            self.send_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
            self.pause_btn.configure(state=tk.DISABLED)
            self.resume_btn.configure(state=tk.NORMAL)
            self.paste_text.configure(state=tk.DISABLED)

    def _update_progress(self, current: int, total: int):
        self.progress_label_var.set(f"{current}/{total}")
        if total > 0:
            self.progress_var.set((current / total) * 100)

    def _format_result_log(self, result: SendResult) -> str:
        timestamp = datetime.now().strftime("%H:%M:%S")
        if result.success:
            return f"✅ Sent to {result.email} — {timestamp}"
        return f"❌ Failed: {result.email} — {timestamp}"

    def _on_send_status(self, email: str):
        self._send_current += 1

        def update():
            self.status_var.set(
                f"Sending to {email}... ({self._send_current}/{self._send_total})"
            )
            self._set_recipient_row(email, STATUS_SENDING)

        self.root.after(0, update)

    def _on_send_result(self, result: SendResult, current: int, total: int):
        def update():
            tag = "success" if result.success else "fail"
            self._append_log(self._format_result_log(result), tag)
            self._send_completed = current
            self._update_progress(current, total)

            if result.success:
                self._set_recipient_row(result.email, STATUS_SENT, sent=True)
            else:
                self._set_recipient_row(result.email, STATUS_FAILED, sent=False)
            self._refresh_recipient_stats()

            # Scroll to the row that was just updated
            key = self._recipient_key(result.email)
            if key in self.recipients_tree.get_children():
                self.recipients_tree.see(key)

        self.root.after(0, update)

    def _on_send_complete(self, stopped: bool, _results: list[SendResult], failed_emails: list[str]):
        def finish():
            if stopped:
                for record in self._recipient_records.values():
                    if record["status"] == STATUS_SENDING:
                        self._set_recipient_row(
                            record["email"], STATUS_UNREAD, sent=record["sent"]
                        )
                self._refresh_recipient_stats()
                self._update_control_states("idle")
                self.status_var.set("Stopped.")
                self._append_log("Sending stopped by user.", "info")
                return

            self.status_var.set("Finished sending.")
            self._append_log("Sending complete.", "info")

            if failed_emails:
                count = len(failed_emails)
                retry = messagebox.askyesno(
                    "Retry Failed Emails",
                    f"{count} email{'s' if count != 1 else ''} failed. Retry?",
                )
                if retry:
                    self._start_retry(failed_emails)
                    return

            self._update_control_states("idle")

        self.root.after(0, finish)

    def _start_retry(self, failed_emails: list[str]):
        self._update_control_states("sending")
        self._send_total = len(failed_emails)
        self._send_current = 0
        self._send_completed = 0
        self.progress_var.set(0)
        self._update_progress(0, len(failed_emails))
        self.status_var.set("Retrying failed emails...")
        self._append_log(
            f"Retrying {len(failed_emails)} failed email(s)...", "info"
        )

        for email in failed_emails:
            self._set_recipient_row(email, STATUS_UNREAD, sent=False)

        if not self._ensure_gmail_login():
            self._update_control_states("idle")
            return

        self._email_sender.start_retry(
            emails=failed_emails,
            subject=self._last_subject,
            body=self._last_body,
            on_status=self._on_send_status,
            on_result=self._on_send_result,
            on_complete=self._on_send_complete,
            gmail_service=self._gmail_service,
        )

    def start(self):
        if self._email_sender.is_running:
            return

        emails = self._get_recipient_emails()
        subject = self.subject_var.get().strip()
        body = self.body_text.get("1.0", tk.END).strip()

        errors = validate_send_inputs(emails, subject, body)
        if errors:
            messagebox.showwarning("Send", "\n".join(errors))
            return

        if not messagebox.askyesno("Confirm Send", f"Send to {len(emails)} recipients?"):
            return

        if not self._ensure_gmail_login():
            return

        self._last_subject = subject
        self._last_body = body
        self._reset_recipients_for_send(emails)

        interval = int(self.config.get("interval_seconds", 30))
        self._email_sender.interval_seconds = interval
        self.interval_label_var.set(f"Interval: {interval}s between emails")

        self._update_control_states("sending")
        self._send_total = len(emails)
        self._send_current = 0
        self._send_completed = 0
        self.progress_var.set(0)
        self._update_progress(0, len(emails))
        self.status_var.set("Starting...")
        self._append_log(f"Starting send to {len(emails)} recipient(s)...", "info")

        if not self._email_sender.start(
            emails=emails,
            subject=subject,
            body=body,
            on_status=self._on_send_status,
            on_result=self._on_send_result,
            on_complete=self._on_send_complete,
            gmail_service=self._gmail_service,
        ):
            self._update_control_states("idle")
            messagebox.showerror("Send", "Could not start sending. Check inputs and try again.")

    def pause(self):
        if not self._email_sender.is_running or self._email_sender.is_paused:
            return

        self._email_sender.pause()
        self._update_control_states("paused")
        self.status_var.set(f"Paused at {self._send_completed}/{self._send_total}")
        self._append_log(
            f"Paused at {self._send_completed}/{self._send_total}.", "info"
        )

    def resume(self):
        if not self._email_sender.is_running or not self._email_sender.is_paused:
            return

        self._email_sender.resume()
        self._update_control_states("sending")
        self.status_var.set("Resuming...")
        self._append_log("Sending resumed.", "info")

    def stop(self):
        if not self._email_sender.is_running:
            return
        self._email_sender.stop()
        self.status_var.set("Stopping...")
        self._append_log("Stop requested — finishing current email...", "info")

    def run(self):
        self.root.mainloop()
