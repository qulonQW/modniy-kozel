"""Shopper API (favorites, orders). Every route needs valid Telegram initData
(router-level dependency); the user ID comes only from it, never the body."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response

from . import repo
from .auth import TelegramUser, require_user
from .deps import get_db
from .ratelimit import limit
from .schemas import PRODUCT_ID_RE, PublicProduct

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

Db = Annotated[sqlite3.Connection, Depends(get_db)]
User = Annotated[TelegramUser, Depends(require_user)]
ProductIdPath = Annotated[str, Path(pattern=PRODUCT_ID_RE)]


def _private(response: Response) -> None:
    # Per-user data: never stored by the WebView or a proxy.
    response.headers["Cache-Control"] = "no-store"


@router.get(
    "/favorites", response_model=list[PublicProduct], dependencies=[Depends(limit("favorites"))]
)
def list_favorites(user: User, conn: Db, response: Response):
    _private(response)
    return repo.list_favorites(conn, user.id)


@router.put(
    "/favorites/{product_id}", status_code=204, dependencies=[Depends(limit("favorites"))]
)
def add_favorite(product_id: ProductIdPath, user: User, conn: Db):
    try:
        with conn:
            if not repo.add_favorite(conn, user.id, product_id):
                raise HTTPException(status_code=404, detail="Product not found")
    except repo.FavoritesFull:
        raise HTTPException(
            status_code=409, detail=f"favorites_full: at most {repo.MAX_FAVORITES} favorites"
        ) from None
    return Response(status_code=204)


@router.delete(
    "/favorites/{product_id}", status_code=204, dependencies=[Depends(limit("favorites"))]
)
def remove_favorite(product_id: ProductIdPath, user: User, conn: Db):
    with conn:
        repo.remove_favorite(conn, user.id, product_id)
    return Response(status_code=204)
