# Runbook — Modniy Kozel Mini App

One FastAPI process serves the storefront (`/`), the admin page (`/admin/`)
and the API (`/api/...`) on one origin. Data: one SQLite file + an uploads
folder. All commands below run on Windows (Git Bash or PowerShell).

## 1. Start locally

```bash
cd backend
python -m venv .venv                                   # first time only
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env                                   # first time only, then fill it in
.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000. A bad `.env` stops the server with a one-line
message naming the variable (never its value). On start the server creates
missing tables/columns; if an existing database needs a change it is first
copied to `backend/data/backup-<UTC time>.sqlite3`.

Browser-only development (no Telegram): `ENV=dev` + `DEV_ADMIN_ID=<your id>`
makes every request act as that user (admin, favorites, orders). The app
refuses to start with `DEV_ADMIN_ID` set when `ENV=prod`.

## 2. Environment variables (backend/.env)

| Name | Meaning |
|---|---|
| `ENV` | `dev` or `prod` |
| `BOT_TOKEN` | token of the bot that opens the Mini App; verifies initData and sends order notifications |
| `ADMIN_TELEGRAM_IDS` | comma-separated numeric Telegram user IDs with admin access; each gets new-order messages |
| `DEV_ADMIN_ID` | dev only: act as this user without Telegram; must be empty in prod |
| `ALLOWED_ORIGIN` | the one origin allowed by CORS, e.g. the tunnel URL; `https://` in prod |
| `DATABASE_PATH` | optional, default `backend/data/shop.sqlite3` |
| `UPLOADS_DIR` | optional, default `backend/uploads` |

Never commit `.env`, the database, `uploads/`, `import/raw/` or `import/private/`
(all in `.gitignore`).

## 3. Test on a phone through a Cloudflare tunnel (test bot)

1. `.env`: `ENV=prod`, `BOT_TOKEN` = token of the **test** bot,
   `ADMIN_TELEGRAM_IDS` = both admins, `DEV_ADMIN_ID` empty.
2. Start the server (section 1), then in a second terminal:
   `cloudflared tunnel --url http://localhost:8000` → copy the `https://….trycloudflare.com` URL.
3. Put that URL into `ALLOWED_ORIGIN` and restart the server
   (a quick tunnel gets a new URL every run: repeat steps 3–4 each time).
4. @BotFather → the test bot → *Bot Settings → Menu Button* (or the Mini App
   URL) → the tunnel URL. Admin page: the same URL + `/admin/`.
5. Each admin opens the test bot once and presses **Start** — otherwise
   Telegram refuses to deliver order notifications to them (shown in the
   admin order page as `http_403`; press «Надіслати ще раз» after Start).

## 4. Switch to the real bot

1. Stop the server. Back up (section 6).
2. `.env`: `BOT_TOKEN` = token of the real bot. `ALLOWED_ORIGIN` = the real
   https origin.
3. @BotFather → the real bot → Menu Button / Mini App URL → the real URL.
4. Start the server. Both admins open the **real** bot and press **Start**.
5. Sessions opened from the test bot stop working (their initData is signed
   with the old token) — reopen the Mini App from the real bot.

## 5. Thumbnails backfill (card images)

```bash
cd backend
.venv/Scripts/python -m app.manage thumbnails
```

Creates ~400 px WebP thumbnails in `UPLOADS_DIR/thumbs/` for every product
photo that has none. Idempotent (a second run changes nothing), originals
are only read, and the database is backed up first whenever something
changes. Admin uploads and `import-json` create thumbnails automatically, so
this is only needed for older photos or after restoring an uploads folder.
First run on ~4 400 photos: ~9 minutes; later runs: seconds.

## 6. Backup

```bash
python tools/backup.py                 # database + uploads -> backend/data/backups/<UTC time>/
python tools/backup.py --db-only       # database only (seconds)
python tools/backup.py --no-thumbs     # skip thumbnails (regenerate with section 5)
python tools/backup.py --dest D:/mk-backups
```

Safe while the server runs (SQLite backup API). Every run makes a new
folder; nothing is deleted. Restore with the server stopped: copy
`shop.sqlite3` back to `DATABASE_PATH` and `uploads/` back to `UPLOADS_DIR`.

## 7. Tests

```bash
cd backend
.venv/Scripts/python -m pytest                                   # everything except the browser test
RUN_E2E=1 .venv/Scripts/python -m pytest                         # + browser e2e (needs Microsoft Edge
                                                                 #   and pip install -r requirements-e2e.txt)
```

Tests never call Telegram (a fake notifier is installed) and use temporary
databases only.

## 8. Where things are

- Order texts for shoppers (labels, confirmation, statuses): `texts.js`
- Bot message to admins: `backend/app/notify.py` (`order_message`)
- initData lifetimes: `backend/app/auth.py` (admin 1 h, shoppers 24 h)
- Rate limits: `backend/app/ratelimit.py`
