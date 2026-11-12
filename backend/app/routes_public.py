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


MAX_IDS = 50


@router.get("/products", response_model=list[PublicProduct], dependencies=[Depends(limit("catalog"))])
def list_products(
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
    ids: Annotated[str | None, Query(max_length=MAX_IDS * 65)] = None,
):
    """Published products (cover image only). ids=a,b,c (max 50) returns just
    those: the storefront uses it for the cart instead of the whole list."""
    _no_cache(response)
    if ids is None:
        return repo.list_published(conn)
    wanted = list(dict.fromkeys(i for i in ids.split(",") if i))
    if len(wanted) > MAX_IDS or not all(_ID.match(i) for i in wanted):
        raise HTTPException(status_code=422, detail=f"ids: up to {MAX_IDS} product ids")
    return repo.list_published(conn, wanted)


@router.get("/me", response_model=Me, dependencies=[Depends(limit("me"))])
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
    popular: bool | None = None,
    limit_: Annotated[int, Query(alias="limit", ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    """Published products: search by name, category filter, sort, pages."""
    _no_cache(response)
    query = search_key(q or "")
    if len(query) > Q_MAX:
        raise HTTPException(status_code=422, detail=f"q: at most {Q_MAX} characters")
    items, total = repo.catalog(conn, query or None, category, sort, limit_, offset, popular)
    return CatalogPage(items=items, total=total)


@router.get(
    "/products/{product_id}", response_model=PublicProduct, dependencies=[Depends(limit("catalog"))]
)
def get_product(product_id: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    _no_cache(response)
    # Drafts, hidden and missing ids all look the same from outside.
    product = repo.get_published(conn, product_id) if _ID.match(product_id) else None
    if product is None:
        raise HTTPException(status_code=404, detail="Not found")
    return product
