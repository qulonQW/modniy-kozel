"""Public storefront API: no auth, published products only, no admin fields."""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from . import repo
from .auth import is_admin
from .db import search_key
from .deps import get_db
from .ratelimit import limit
from .schemas import PRODUCT_ID_RE, CatalogPage, Category, Me, PublicProduct

router = APIRouter(prefix="/api")

_ID = re.compile(PRODUCT_ID_RE)


def _no_cache(response: Response) -> None:
    # The storefront must see admin changes on the next reload.
    response.headers["Cache-Control"] = "no-cache"


@router.get("/products", response_model=list[PublicProduct])
def list_products(response: Response, conn: sqlite3.Connection = Depends(get_db)):
    _no_cache(response)
    return repo.list_published(conn)


@router.get("/me", response_model=Me)
def me(request: Request, response: Response):
    """Never 401/403: shoppers without initData or with a non-admin ID just
    get is_admin=false, so the storefront works the same for them."""
    # Per-user answer: must not be stored by the WebView or any proxy.
    response.headers["Cache-Control"] = "no-store"
    return Me(is_admin=is_admin(request))


Q_MAX = 50


@router.get("/catalog", response_model=CatalogPage, dependencies=[Depends(limit("catalog"))])
def catalog(
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
    q: Annotated[str | None, Query(max_length=200)] = None,
    category: Category | None = None,
    sort: Literal["popular", "price_asc", "price_desc", "newest"] = "popular",
    limit_: Annotated[int, Query(alias="limit", ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    """Published products: search by name, category filter, sort, pages."""
    _no_cache(response)
    query = search_key(q or "")
    if len(query) > Q_MAX:
        raise HTTPException(status_code=422, detail=f"q: at most {Q_MAX} characters")
    items, total = repo.catalog(conn, query or None, category, sort, limit_, offset)
    return CatalogPage(items=items, total=total)


@router.get("/products/{product_id}", response_model=PublicProduct)
def get_product(product_id: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    _no_cache(response)
    # Drafts, hidden and missing ids all look the same from outside.
    product = repo.get_published(conn, product_id) if _ID.match(product_id) else None
    if product is None:
        raise HTTPException(status_code=404, detail="Not found")
    return product
