"""Shopper API (favorites, orders). Every route needs valid Telegram initData
(router-level dependency); the user ID comes only from it, never the body."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse

from . import notify, orders, repo
from .auth import TelegramUser, require_user
from .config import Settings
from .deps import get_db, get_settings
from .ratelimit import limit
from .schemas import PRODUCT_ID_RE, OrderCreate, OrderOut, PublicProduct

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


# --- orders -------------------------------------------------------------------


@router.post("/orders", response_model=OrderOut, dependencies=[Depends(limit("orders"))])
def create_order(
    body: OrderCreate,
    user: User,
    conn: Db,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
):
    """201 for a new order, 200 when the idempotency key already has one.
    409 cart_changed / price_changed: nothing was created."""
    _private(response)
    try:
        order_id, created = orders.create_order(conn, user, body, settings.admin_telegram_ids)
    except orders.CartProblem as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Some items can no longer be ordered",
                "code": "cart_changed",
                "problems": exc.problems,
            },
            headers={"Cache-Control": "no-store"},
        )
    except orders.PriceChanged as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Prices changed; confirm the new total",
                "code": "price_changed",
                "total": exc.total,
                "prices": exc.prices,
            },
            headers={"Cache-Control": "no-store"},
        )
    if created:
        response.status_code = 201
        # Runs after the response is sent; the order is already committed.
        background.add_task(notify.run_due, request.app, order_id)
    return orders.get_own(conn, user.id, order_id)


@router.get("/orders/mine", response_model=list[OrderOut], dependencies=[Depends(limit("orders_read"))])
def my_orders(user: User, conn: Db, response: Response):
    _private(response)
    return orders.list_own(conn, user.id)
