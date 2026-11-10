"""Public storefront API: no auth, published products only, no admin fields."""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response

from . import repo
from .deps import get_db
from .schemas import PRODUCT_ID_RE, PublicProduct

router = APIRouter(prefix="/api")

_ID = re.compile(PRODUCT_ID_RE)


def _no_cache(response: Response) -> None:
    # The storefront must see admin changes on the next reload.
    response.headers["Cache-Control"] = "no-cache"


@router.get("/products", response_model=list[PublicProduct])
def list_products(response: Response, conn: sqlite3.Connection = Depends(get_db)):
    _no_cache(response)
    return repo.list_published(conn)


@router.get("/products/{product_id}", response_model=PublicProduct)
def get_product(product_id: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    _no_cache(response)
    # Drafts, hidden and missing ids all look the same from outside.
    product = repo.get_published(conn, product_id) if _ID.match(product_id) else None
    if product is None:
        raise HTTPException(status_code=404, detail="Not found")
    return product
