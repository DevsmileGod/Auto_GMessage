# 🚀 Auto_GMessage

![GitHub stars](https://img.shields.io/github/stars/DevsmileGod/Auto_GMessage?style=social)
![GitHub forks](https://img.shields.io/github/forks/DevsmileGod/Auto_GMessage?style=social)
![GitHub last commit](https://img.shields.io/github/last-commit/DevsmileGod/Auto_GMessage)
![GitHub repo size](https://img.shields.io/github/repo-size/DevsmileGod/Auto_GMessage)
![License](https://img.shields.io/github/license/DevsmileGod/Auto_GMessage)

# Gmail Auto Sender

A desktop app that sends Gmail messages automatically on a fixed interval (default **30 seconds**). Built with Python, Tkinter, and the Gmail API.

## Features

- Bulk send to 100+ recipients (paste list or load CSV)
- OAuth2 login with token reuse (`token.json`)
- Live progress, pause/resume, and retry for failed emails
- Email templates saved to `templates.json`
- Dark/light mode and configurable send interval
- Session logs in `logs/session_YYYY-MM-DD.log`

## Requirements

- Python **3.10+**
- A Google Cloud project with Gmail API enabled
- OAuth 2.0 Desktop credentials (`credentials.json`)

## Quick Start

### Windows

```bat
run.bat
```

### macOS / Linux

```bash
chmod +x run.sh
./run.sh
```

### Manual run

```bash
pip install -r requirements.txt
python main.py
```

## Google Cloud Setup (`credentials.json`)

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or select an existing one).
3. Enable the **Gmail API**:
   - APIs & Services → **Library**
   - Search **Gmail API** → **Enable**
4. Configure the OAuth consent screen:
   - APIs & Services → **OAuth consent screen**
   - Choose **External** (or Internal for Workspace)
   - Fill in app name, support email, and developer contact
   - Add scope: `https://www.googleapis.com/auth/gmail.send`
   - Add your Gmail address as a **Test user** (while in Testing mode)
5. Create OAuth credentials:
   - APIs & Services → **Credentials**
   - **Create Credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file
6. Rename the downloaded file to `credentials.json` and place it in this project folder.

On first run, a browser window opens for Google sign-in. After approval, `token.json` is created so you are not prompted every time.

### Use a different Gmail account

1. Click **Switch Account** in the app toolbar (or delete `token.json` manually).
2. Click **Sign in with Google** and choose the other Gmail in the browser.
3. Click **Allow** — all sends use that account until you switch again.

## Project Structure

```
gmail-auto-sender/
├── main.py           # Entry point
├── auth.py           # Gmail OAuth2 authentication
├── sender.py         # Email sending + retry logic
├── ui.py             # Tkinter GUI
├── config.json       # App settings (interval, theme)
├── templates.json    # Saved email templates
├── credentials.json  # Google OAuth client (you provide)
├── token.json        # Saved login token (auto-generated)
├── requirements.txt
├── run.bat           # Windows launcher
├── run.sh            # macOS/Linux launcher
└── logs/             # App and session logs
```

## Configuration

Edit `config.json` or use **Settings** in the app:

| Setting            | Default | Description                    |
|--------------------|---------|--------------------------------|
| `interval_seconds` | `30`    | Seconds between each email     |
| `theme`            | `light` | UI theme (`light` or `dark`)   |

Environment variables (optional, in `.env`):

```env
GOOGLE_CREDENTIALS_FILE=credentials.json
```

## Recipient list

The **Recipients** panel shows each address in a table:

| Column | Meaning |
|--------|---------|
| **Sent** | ☑ = successfully sent, ☐ = not sent yet |
| **#** | Row number |
| **Email** | Recipient address |
| **Status** | `Unread` (pending), `Sending`, `Sent`, or `Failed` |

Stats below the list: `Total | Sent | Unread | Failed`

Paste emails → **Import**, or use **Load CSV**. Status updates live as each message is sent.

Select one or many rows (Ctrl+click / Shift+click), then **Delete Selected** or press the **Delete** key. **Clear All** removes every recipient.

> **Note:** Status reflects send progress in this app, not whether the recipient opened the email in Gmail (that is not available via the Gmail API for bulk sends).

## CSV Import

CSV files must include a column named `email`:

```csv
email,name
alice@example.com,Alice
bob@example.com,Bob
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `credentials.json` not found | Download OAuth Desktop credentials from Google Cloud Console |
| `access_denied` on login | Add your Gmail as a test user on the OAuth consent screen |
| Gmail API quota errors | Reduce send volume or increase interval in Settings |
| Token expired | Delete `token.json` and sign in again |

## Security Notes

- Do **not** commit `credentials.json`, `token.json`, or `.env` to git
- Keep OAuth credentials private
- Use test mode and test users during development

## License

MIT
# Auto_GMessage
