"""App factory. Tests build apps with explicit Settings; app.main builds the
real one from .env."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from . import errors, notify, routes_admin, routes_public, routes_user
from .brand import load_brand
from .config import REPO_DIR, Settings, validate_settings
from .db import connect, init_db
from .ratelimit import build_limiters
from .storefront import Storefront


IMMUTABLE_IMAGE_PREFIXES = ("/uploads/thumbs/", "/uploads/products/")
DEMO_IMAGE_PREFIXES = ("/public/images/", "/images/products/")

# The shop's own files (repo root); the brand adds brand/theme.css,
# brand/texts.js and brand/logo.png (app/brand.py).
STOREFRONT_FILES = (
    "index.html", "catalog.html", "product.html", "cart.html", "checkout.html", "orders.html",
    "favorites.html", "app.js", "cart.js", "products.js", "style.css",
)

# No inline scripts; only our own files, the Telegram SDK and Google Fonts.
# frame-ancestors lets Telegram Web embed the Mini App in its iframe.
ADMIN_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self' https://telegram.org",
        "style-src 'self' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' blob: data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
    ]
)


def create_app(settings: Settings) -> FastAPI:
    validate_settings(settings)  # raises ConfigError before anything is served
    brand = load_brand(settings.brand)  # same: a broken brand folder never serves

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(settings.database_path)
        try:
            init_db(conn)
        finally:
            conn.close()
        retry = asyncio.create_task(notify.retry_loop(app))
        try:
            yield
        finally:
            retry.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retry

    is_dev = settings.env == "dev"
    app = FastAPI(
        title=f"{brand.name} API",
        lifespan=lifespan,
        docs_url="/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if is_dev else None,
    )
    app.state.settings = settings
    app.state.brand = brand
    # Tests replace this with a fake; None (no token, dev) keeps notifications pending.
    app.state.notifier = notify.BotNotifier(settings.bot_token) if settings.bot_token else None

    if settings.allowed_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.allowed_origin],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
            allow_headers=["Content-Type", "X-Telegram-Init-Data"],
            max_age=600,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = ADMIN_CSP
            response.headers["Cache-Control"] = "no-cache"
        elif response.status_code == 200 and path.startswith(IMMUTABLE_IMAGE_PREFIXES):
            # Content-addressed thumbnails and random/hash-named uploads are
            # never overwritten under the same name.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif response.status_code == 200 and path.startswith(DEMO_IMAGE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    # JSON lists and the storefront's HTML/JS/CSS; images are already compressed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    errors.install(app)  # one error format; JSON body size limit
    app.state.limiters = build_limiters()
    app.include_router(routes_public.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_user.router)

    # Image files only. Never mount the repo root: it contains backend/.env.
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=settings.uploads_dir), name="uploads")
    app.mount(
        "/public/images/products",
        StaticFiles(directory=REPO_DIR / "public" / "images" / "products", check_dir=False),
        name="demo-images",
    )
    app.mount(
        "/images/products",
        StaticFiles(directory=REPO_DIR / "images" / "products", check_dir=False),
        name="imported-images",
    )
    # Admin page (static HTML/JS/CSS). Data still needs initData on /api/admin.
    # Its index.html gets the brand's name/fonts; the rest is served as is.
    admin_page = Storefront({"index.html": REPO_DIR / "admin" / "index.html"}, brand.html_values())

    @app.get("/admin/", include_in_schema=False)
    @app.get("/admin/index.html", include_in_schema=False)
    def admin_index(request: Request):
        return admin_page.response(request, "index.html")

    app.mount("/admin", StaticFiles(directory=REPO_DIR / "admin", html=True), name="admin")

    # Storefront: an explicit allowlist of files from the repo root (the root
    # itself also holds backend/, tools/ and import/, which must never be served).
    # Registered last so it cannot shadow /api, /admin or the image mounts.
    # HTML gets versioned asset URLs; versioned assets are cached for a year
    # (app/storefront.py), so page changes inside Telegram skip JS/CSS.
    shop_files = Storefront(
        {**{name: REPO_DIR / name for name in STOREFRONT_FILES}, **brand.files()},
        brand.html_values(),
    )

    @app.get("/", include_in_schema=False)
    @app.get("/{name}", include_in_schema=False)
    @app.get("/brand/{name}", include_in_schema=False)
    def storefront(request: Request, name: str = "index.html"):
        if request.url.path.startswith("/brand/"):
            name = f"brand/{name}"
        elif name == "admin":  # this route would otherwise swallow the mount's redirect
            return RedirectResponse("/admin/", status_code=308)
        if name not in shop_files:
            raise HTTPException(status_code=404, detail="Not found")
        return shop_files.response(request, name)

    return app
