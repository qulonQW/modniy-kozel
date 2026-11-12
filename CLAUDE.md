# Telegram Mini App shop — guide for Claude

One codebase, many shops. Everything that makes a shop *this* shop (name,
logo, colours, fonts, shopper texts) lives in `brands/<slug>/`; the rest is
the shared core. The active shop is `BRAND=<slug>` in `backend/.env`.

Conventions: chat with the owner in Russian; code, comments and commits in
English; shopper and admin UI text in Ukrainian.

## Map

```
brands/<slug>/          ONE SHOP (the only place a shop is named)
  brand.json            name, short_name (≤4), hero_lines (1–3), tagline, lang, fonts_url
  theme.css             colours, fonts, radii: CSS variables only
  texts.js              shopper texts for cart / checkout / orders (TEXTS)
  logo.png              square logo: header + browser icon
  logo-full.jpg         (optional, not served) source artwork

*.html, app.js, cart.js, products.js, style.css   storefront core (all shops)
admin/                  admin panel core (own admin.css, Ukrainian)
backend/app/            FastAPI + SQLite core
  brand.py              loads/validates brands/<slug>/ at startup
  storefront.py         serves pages: fills {{brand.*}}, versions assets
  application.py        app factory, routes, static mounts, security headers
  routes_public.py      catalog/products for shoppers (never drafts, never supplier price)
  routes_user.py        favorites, orders (user id ONLY from Telegram initData)
  routes_admin.py       admin CRUD, uploads, orders
  orders.py notify.py   order creation (server-side prices) + bot message to admins
  importer.py           supplier Telegram-export importer — do not change its logic casually
backend/tests/          pytest; test_brand.py guards this structure
tools/                  import_products.py + import_config.json (suppliers are per shop), backup.py
deploy/                 VPS: setup.sh, update.sh, import-data.sh, systemd, Caddy
RUNBOOK.md              how to run, test, deploy, back up
```

## How a page gets the brand

Pages contain placeholders, the server fills them (escaped) for the active brand:

| In HTML                         | Becomes                                   |
|---------------------------------|-------------------------------------------|
| `{{brand.name}}`                | `brand.json` → name                       |
| `{{brand.short_name}}`          | short name (logo alt / fallback)          |
| `{{brand.hero}}`                | `hero_lines` joined with `<br>`           |
| `{{brand.tagline}}`, `{{brand.lang}}`, `{{brand.fonts_url}}` | same keys |
| `./brand/theme.css`, `./brand/texts.js`, `./brand/logo.png` | files of the active brand |

In JS use `MK.shopName` (from `<meta name="application-name">`), `TEXTS.*` for
shopper texts. An unknown placeholder fails loudly; a broken brand folder
stops the app at startup with a message naming the problem.

## Recipes

**New shop** (same code, another brand):
1. `cp -r brands/modniy-kozel brands/<new-slug>` (lowercase, digits, `-`).
2. Edit `brand.json`, replace `logo.png` (square PNG, ≥ 112 px), adjust `theme.css` values, reword `texts.js` (keep keys and `${...}` placeholders).
3. Set `BRAND=<new-slug>` in `backend/.env` (on a server: same file, then `systemctl restart tovarka`).
4. Run tests. Suppliers for the importer: `tools/import_config.json`. Own bot token and admins in `.env`.

**Rename / new logo / new colours of an existing shop**: only its `brands/<slug>/` files.

**New look (beyond colours)**: change `style.css` (layout/components for every
shop). Colours there must stay `var(--...)`; a new variable must be added to
EVERY `brands/*/theme.css` (a test checks this).

**New shopper text**: add the key to EVERY `brands/*/texts.js` (a test checks
that all brands have the same keys), then use `TEXTS.<group>.<key>` in the page.

## Rules that keep it from breaking

- Never write a shop's name, hero words or colours outside `brands/` (tests fail).
- New storefront file → add it to `STOREFRONT_FILES` in `backend/app/application.py`; nothing else from the repo root is served.
- Build DOM with `MK.el` / `textContent`; never `innerHTML` (tests check).
- Security invariants: user id only from verified initData; prices computed on the server; public API never exposes `price_supplier`, drafts, hidden items or review flags; bot messages html-escape user text.
- DB schema: additive only (`ADDED_TABLES` / `ADDED_COLUMNS` in `db.py`); back up first.
- Never commit `backend/.env`, `*.sqlite3`, `backend/uploads/`, `import/raw/`, `import/private/`.
- Before committing: `cd backend && .venv/Scripts/python -m pytest -q` (Windows) — all green.
