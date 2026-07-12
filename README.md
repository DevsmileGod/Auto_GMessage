# 🚀 Auto_GMessage — Gmail Auto Sender

![GitHub stars](https://img.shields.io/github/stars/DevsmileGod/Auto_GMessage?style=social)
![GitHub forks](https://img.shields.io/github/forks/DevsmileGod/Auto_GMessage?style=social)
![GitHub last commit](https://img.shields.io/github/last-commit/DevsmileGod/Auto_GMessage)
![GitHub repo size](https://img.shields.io/github/repo-size/DevsmileGod/Auto_GMessage)
![License](https://img.shields.io/github/license/DevsmileGod/Auto_GMessage)

A desktop app that runs a two-phase Gmail outreach campaign: it sends a **unique first message** to each recipient, then **watches your inbox and auto-replies** to anyone who responds.

Sign in with one click via **Sign in with Google** (OAuth), or with a classic **App Password**. Sending goes over SMTP; reply detection over IMAP. Either way the saved secret is encrypted at rest with Windows DPAPI.

## How a campaign works

**Phase 1 — Outreach.** The **first-message pool** is a list of distinct emails (subject + body). Each recipient, in order, gets the next *available* one:

```
Pool: [A, B, C]        Recipients: R1, R2, R3
R1 ← A   (A locked 24h)
      … wait (interval) …
R2 ← B   (B locked 24h)
      … wait (interval) …
R3 ← C   (C locked 24h)
```

No two recipients receive the same text, and **a sent message is locked for 24 hours** so it is never reused within a day (the lock is saved to disk).

**Batching for large lists.** If you have more recipients than ready messages — say 100 recipients but 18 messages — the app contacts the first 18, then **parks and drips the rest in daily batches**: each time a message's 24h lock expires it is reused for the next waiting recipient. A cursor (the ▸ marker in the list) records exactly who is next.

**It survives sleep and shutdown.** Progress is saved after every send, and "is a message ready yet?" is computed from saved timestamps — not a live timer. So you can close the laptop between batches; when you reopen the app it shows **Resume campaign**, and any batch whose 24h has elapsed while you were away goes out immediately. Nothing is lost, nothing double-sends.

**Phase 2 — Follow-up.** Once outreach is done the app polls your inbox. When a recipient **replies**, it automatically sends your single **second message** back **as a real reply** — threaded into the same conversation (`Re:` subject, body only, exactly like clicking Reply). It keeps watching until every replier has been answered, or until you click **Stop**.

## Setup

**Enable IMAP first, whichever sign-in you use:** Gmail → **Settings → Forwarding and POP/IMAP → Enable IMAP**. *(Without it, outreach still works but replies can't be detected.)*

Then pick a sign-in method.

### Sign in with Google (recommended)

Click the button, pick your account in the browser, done — nothing to copy, and it stays signed in. Google requires every app to identify itself with its own OAuth client, so there is a **one-time setup** to create yours:

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com).
2. **APIs & Services → OAuth consent screen** → *External* → add your own Gmail address under **Test users**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** → Application type: **Desktop app**.
4. Download the JSON and save it beside the app as **`client_secret.json`**.

Now **Sign in with Google** works. The app never sees your Google password.

> **Publish the consent screen** (*OAuth consent screen → Publish app*) once it works. While it stays in *Testing*, Google expires the saved authorisation after **7 days** and you'll have to click sign-in again. Publishing an app using the `mail.google.com` scope to *outside* users would need Google's security review — but you don't need that here, because you are the only user.

### App Password (fallback)

1. Turn on **2-Step Verification** at [myaccount.google.com/security](https://myaccount.google.com/security).
2. Create an App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Launch the app, click **Sign in**, and paste it in the lower half of the dialog. Spaces are ignored.

With *Remember on this computer* ticked, the login is written to `gmail_credentials.json` (gitignored). The secret inside — refresh token or app password — is **encrypted with Windows DPAPI**, so the file is useless to another Windows user or on another machine. **Sign out** deletes it.

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

1. **Sign in** — one click with Google, or with an App Password.
2. Build the **first-message pool** (left panel, top). Use **Import…** to add many at once; **New** for a single one. **Duplicate** copies a message as an unlocked variant to edit, and **Delete** works on a multi-selection. You can have far fewer messages than recipients — they'll drip in daily batches. **Clear locks** removes the 24h cooldowns if you need to reuse messages sooner.

   **Import…** takes messages three ways:

   - **Paste** them into the box, separated by a line of three dashes. In each one the first line is the subject and the rest is the body — so blank lines inside a body are safe:

     ```text
     Quick question about your rollout
     Hi there,

     Saw the announcement and wanted to reach out.
     ---
     Following up on your hiring plans
     Hi there,

     Different message, different subject.
     ```

   - **Load CSV…** — any CSV with `subject` and `body` columns (a headerless file is read as subject, body).
   - **Load folder…** — every `.txt`/`.md` file in it becomes one message: first line subject, rest body.
3. Write the **second (reply) message** (left panel, bottom) — body only — and click **Save reply**.
4. Paste recipient addresses on the right (one per line, or comma/semicolon separated) and click **Import**. Or **Load CSV** with an `email` column.
5. Set the outreach interval and the inbox-poll interval under **Settings**.
6. Click **Start campaign**.

**Pause** freezes the campaign without losing its place; **Resume** continues. **Stop** halts it — progress is saved, so the button becomes **Resume campaign** and you can pick up later (including after closing the app entirely). Closing the window mid-campaign is safe for the same reason.

The **Status** column moves `Pending → Sending → Sent → Replied → Done` (or `Failed`). "Sent" means contacted and awaiting a reply; "Done" means the recipient replied and received the follow-up. The **▸** marker shows the resume point — the next recipient to be contacted.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `ui.py` | Tkinter window, message manager, recipient list, controls |
| `sender.py` | The resumable two-phase `Campaign` (batched outreach, then watch-and-reply) |
| `message_store.py` | First-message pool + 24h locks, second message, persistence |
| `campaign_state.py` | The resume flag: recipient queue, cursor, per-recipient status |
| `gmail_client.py` | SMTP: login (OAuth or App Password), sending, threaded replies |
| `imap_client.py` | IMAP: watches the inbox, matches replies to what we sent |
| `google_auth.py` | "Sign in with Google": OAuth consent flow, token refresh, XOAUTH2 |
| `secret_store.py` | Encrypts the saved secret at rest (Windows DPAPI) |
| `config.json` | Interval, poll interval, theme |
| `messages.json` | First-message pool (with lock timestamps) and the reply message |
| `campaign_state.json` | Saved campaign progress, so it resumes after a restart |
| `gmail_credentials.json` | Your saved login, secret encrypted (gitignored, created on first sign-in) |
| `client_secret.json` | Your Google OAuth client, if you use Google sign-in (gitignored, you provide it) |
| `logs/` | `app.log` plus a per-day session log |

## Sending limits

Gmail caps a normal account at roughly **500 recipients per day** (about 2,000 for Workspace). Exceeding it gets the account temporarily blocked from sending. Keep the interval reasonably long — rapid bulk mail is what spam filters look for, which is exactly why the first-message pool sends everyone different text.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

94 tests. `tests/test_end_to_end.py` runs a real SMTP server in-process and drives the actual GUI through a full campaign — outreach and threaded auto-reply — with nothing mocked below the socket (replies are injected through a scripted inbox, since a live IMAP server isn't practical in tests).
