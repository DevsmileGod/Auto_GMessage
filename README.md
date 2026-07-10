# 🚀 Auto_GMessage — Gmail Auto Sender

![GitHub stars](https://img.shields.io/github/stars/DevsmileGod/Auto_GMessage?style=social)
![GitHub forks](https://img.shields.io/github/forks/DevsmileGod/Auto_GMessage?style=social)
![GitHub last commit](https://img.shields.io/github/last-commit/DevsmileGod/Auto_GMessage)
![GitHub repo size](https://img.shields.io/github/repo-size/DevsmileGod/Auto_GMessage)
![License](https://img.shields.io/github/license/DevsmileGod/Auto_GMessage)

A desktop app that sends **two messages to each recipient**, one recipient at a time, with a fixed delay between every message.

Sign-in uses your Gmail address and a Google **App Password** over SMTP — no OAuth, no Google Cloud project, no `credentials.json`.

## How sending works

For every recipient, in list order:

```
alice@example.com   ← message 1
                    ← wait (interval)
alice@example.com   ← message 2
                    ← wait (interval)
bob@example.com     ← message 1
                    ← wait (interval)
bob@example.com     ← message 2
```

Both of Alice's messages are delivered before Bob is contacted at all. There is no wait after the final message.

If a recipient's **first** message fails, their second is skipped — there is no point following up on a message that never arrived. That recipient is offered for retry, and a retry only resends the messages that never landed, so nobody receives a duplicate.

## Setup

Google stopped accepting normal account passwords over SMTP, so you need a 16-character App Password:

1. Turn on **2-Step Verification** at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Create an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Launch the app, click **Sign in**, and paste it. Spaces are ignored.

With *Remember on this computer* ticked, the address and password are written to `gmail_credentials.json`, which is gitignored. **Sign out** deletes that file.

## Running

**Windows, no install:** double-click `GmailAutoSender.exe`. Nothing else is needed — Python is bundled inside. Its settings, saved login, and logs are written next to the exe (or under `%APPDATA%\GmailAutoSender` if the exe sits in a read-only folder like Program Files).

**From source:**

```bash
python main.py
```

Or use `run.bat` (Windows) / `run.sh` (macOS, Linux). There are no runtime dependencies — the app uses only the Python standard library (`smtplib`, `tkinter`, `json`). Python 3.10+.

### Building the exe yourself

```bash
pip install pyinstaller
python -m PyInstaller GmailAutoSender.spec --noconfirm --clean
```

The result is a single `dist/GmailAutoSender.exe` (~11 MB).

## Using it

1. **Sign in** with your Gmail address and App Password.
2. Fill in the **Message 1** and **Message 2** tabs. Both need a subject and a body.
3. Paste recipient addresses into the box on the right (one per line, or comma/semicolon separated) and click **Import**. Or **Load CSV** with an `email` column.
4. Set the delay under **Settings** (5–3600 seconds).
5. Click **Send**.

**Pause** freezes the countdown without losing your place; **Resume** picks it up. **Stop** finishes the message currently in flight, then halts.

The **Status** column moves `Unread` → `Sending` → `1 of 2` → `Sent`. A recipient is only ticked as sent once *both* messages have gone out.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `ui.py` | Tkinter window, recipient list, controls |
| `sender.py` | The two-messages-per-recipient loop, pause/stop/retry |
| `gmail_client.py` | SMTP connection, login, message building |
| `config.json` | Interval, theme, and the last messages you typed |
| `templates.json` | Saved message pairs |
| `gmail_credentials.json` | Your saved login (gitignored, created on first sign-in) |
| `logs/` | `app.log` plus a per-day session log |

## Sending limits

Gmail caps a normal account at roughly **500 recipients per day** (about 2,000 for Workspace). Because every recipient gets two messages, that works out to ~250 recipients per day. Exceeding it gets the account temporarily blocked from sending.

Keep the interval reasonably long. Rapid identical bulk mail is what spam filters look for.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

81 tests. `tests/test_end_to_end.py` runs a real SMTP server in-process and drives the actual GUI through a full send — nothing below the socket is mocked.
