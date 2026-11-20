# Mr. Downloading Bot

Telegram bot that turns an Instagram or YouTube link into an MP3 and an MP4.

Authorized users send a link. The bot downloads the video once, extracts the audio with ffmpeg, and uploads both files. Anyone else has to be approved by the owner first.

## Features

- Instagram posts, reels, and TV links, plus YouTube watch, short, and `youtu.be` links
- One video download, then an MP3 extracted locally (falls back to a separate audio download)
- Owner approve / decline buttons, saved across restarts
- `/users` and `/revoke` for the owner
- Skips files larger than Telegram's 50 MB bot upload limit instead of failing halfway
- Optional `cookies.txt` for login-walled Instagram posts

## Requirements

- Python 3.10 or newer
- ffmpeg on `PATH` (needed to merge video and to make the MP3)
- A bot token from [@BotFather](https://t.me/BotFather)
- Your numeric Telegram user id (send `/start` to [@userinfobot](https://t.me/userinfobot))

## Setup

```bash
git clone https://github.com/MarcDennis/Mr.DownloadingBot.git
cd Mr.DownloadingBot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
BOT_TOKEN=123456:replace-with-the-token-from-botfather
OWNER_ID=123456789
APPROVED_USERS_FILE=approved_users.json
# COOKIES_FILE=/absolute/path/cookies.txt
```

Never commit `.env`, a real token, or `approved_users.json`.

```bash
python bot.py
```

Open the bot, send `/start`, then paste a public link.

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/start` | anyone | Welcome if approved, otherwise one access request to the owner |
| `/help` | anyone | Short usage text |
| `/users` | owner | List approved user ids |
| `/revoke <id>` | owner | Remove a user's access |
| any text | approved user | If it contains a supported link, download audio and video |

Approve or decline from the buttons the bot sends you. A second `/start` while a request is waiting does not ping you again.

## Environment

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `BOT_TOKEN` | yes | — | Telegram bot token |
| `OWNER_ID` | yes | — | Numeric Telegram id that can approve users |
| `APPROVED_USERS_FILE` | no | `approved_users.json` | JSON list of approved ids. On Railway use `/data/approved_users.json` and mount a volume at `/data` |
| `COOKIES_FILE` | no | empty | Netscape cookies file for private or login-walled Instagram URLs |

## Deploy

**Railway**

1. Push this repo and create a service from it.
2. Set `BOT_TOKEN`, `OWNER_ID`, and `APPROVED_USERS_FILE=/data/approved_users.json`.
3. Add a volume mounted at `/data` so approvals survive restarts.
4. `nixpacks.toml` installs ffmpeg. `railway.json` starts `python bot.py`.

**Heroku-style worker**

`Procfile` runs `worker: python bot.py`. The dyno image still needs ffmpeg, and the filesystem is ephemeral unless you store `APPROVED_USERS_FILE` somewhere persistent.

## Limits

- Bot uploads top out around 50 MB. Larger files are reported and skipped.
- Instagram often blocks datacenter IPs or asks for a login. Export a Netscape `cookies.txt` from a logged-in browser and set `COOKIES_FILE`.
- Playlists are ignored. Only the link you send is downloaded.
- YouTube and Instagram terms still apply. Use this for content you are allowed to download.

## Project layout

```text
bot.py            handlers, approval list, download
requirements.txt  pinned runtime deps
.env.example      placeholders only
nixpacks.toml     ffmpeg for Railway
railway.json      start command
Procfile          worker process
```

## Security

`.env.example` must contain placeholders only. If a real bot token was ever committed, revoke it in BotFather (`/revoke`) and put the new token in the host's environment, not in git.
