"""Admin API. Every route is rate limited and requires a verified admin
(router-level dependencies, so a new route cannot forget them)."""

from __future__ import annotations

import secrets
import sqlite3
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import notify, orders, repo
from .auth import require_admin
from .deps import get_db, get_settings
from .config import Settings
from .images import MAX_IMAGE_BYTES, delete_stored_image, detect_image_type, save_image
from .ratelimit import limit
from .schemas import (
    MAX_IMAGES,
    PRODUCT_ID_RE,
    AdminOrder,
    AdminOrderList,
    AdminProduct,
    AdminProductList,
    BulkStatus,
    Category,
    ImageOrder,
    OrderStatus,
    OrderStatusUpdate,
    ProductCreate,
    ProductUpdate,
    Status,
)

router = APIRouter(
    prefix="/api/admin",
    dependencies=[Depends(limit("admin")), Depends(require_admin)],
)

Db = Annotated[sqlite3.Connection, Depends(get_db)]
ProductIdPath = Annotated[str, Path(pattern=PRODUCT_ID_RE)]

# Multipart framing overhead allowed on top of the image itself.
MAX_UPLOAD_REQUEST = MAX_IMAGE_BYTES + 64 * 1024


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Product not found")


def _get_or_404(conn: sqlite3.Connection, product_id: str) -> AdminProduct:
    product = repo.admin_get(conn, product_id)
    if product is None:
        raise _not_found()
    return product


@router.get("/products", response_model=AdminProductList)
def list_products(
    conn: Db,
    status: Status | None = None,
    category: Category | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    needs_review: bool | None = None,
    duplicates: bool = False,
    limit_: Annotated[int, Query(alias="limit", ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    items, total = repo.admin_list(
        conn, status, category, (search or "").strip() or None, limit_, offset, needs_review,
        duplicates,
    )
    return AdminProductList(items=items, total=total)


PUBLISH_BLOCKED = HTTPException(
    status_code=409,
    detail="publish_blocked: product needs review or has no retail price",
)


@router.get("/products/{product_id}", response_model=AdminProduct)
def get_product(product_id: ProductIdPath, conn: Db):
    return _get_or_404(conn, product_id)


@router.post("/products", response_model=AdminProduct, status_code=201)
def create_product(body: ProductCreate, conn: Db):
    if body.status == "published" and body.price_retail <= 0:
        raise PUBLISH_BLOCKED
    with conn:
        for _ in range(5):
            product_id = f"p-{secrets.token_hex(4)}"
            if conn.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone() is None:
                break
        else:  # pragma: no cover - 2^32 space
            raise HTTPException(status_code=500, detail="Could not allocate id")
        repo.admin_create(conn, product_id, body)
    return _get_or_404(conn, product_id)


@router.patch("/products/{product_id}", response_model=AdminProduct)
def update_product(product_id: ProductIdPath, body: ProductUpdate, conn: Db):
    changes = {name: getattr(body, name) for name in body.model_fields_set}
    try:
        with conn:
            if not repo.admin_update(conn, product_id, changes):
                raise _not_found()
    except repo.PublishBlocked:
        raise PUBLISH_BLOCKED from None
    return _get_or_404(conn, product_id)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(
    product_id: ProductIdPath, conn: Db, settings: Annotated[Settings, Depends(get_settings)]
):
    with conn:
        paths = [r["path"] for r in repo.admin_images(conn, product_id)]
        if conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount == 0:
            raise _not_found()
    for path in paths:  # files go only after the rows are gone
        delete_stored_image(settings.uploads_dir, path)
    return Response(status_code=204)


@router.post("/products/bulk-status")
def bulk_status(body: BulkStatus, conn: Db):
    """Products that cannot be published are left unchanged and listed in "blocked"."""
    with conn:
        updated, blocked = repo.bulk_set_status(conn, list(dict.fromkeys(body.ids)), body.status)
    return {"updated": updated, "blocked": blocked}


@router.put("/products/{product_id}/images/order", response_model=AdminProduct)
def reorder_images(product_id: ProductIdPath, body: ImageOrder, conn: Db):
    with conn:
        _get_or_404(conn, product_id)
        current = [r["id"] for r in repo.admin_images(conn, product_id)]
        if len(body.image_ids) != len(current) or set(body.image_ids) != set(current):
            raise HTTPException(
                status_code=422, detail="image_ids must list every image of the product exactly once"
            )
        repo.renumber_images(conn, product_id, body.image_ids)
        repo.touch(conn, product_id)
    return _get_or_404(conn, product_id)


@router.delete("/products/{product_id}/images/{image_id}", response_model=AdminProduct)
def delete_image(
    product_id: ProductIdPath,
    image_id: int,
    conn: Db,
    settings: Annotated[Settings, Depends(get_settings)],
):
    with conn:
        row = conn.execute(
            "SELECT path FROM product_images WHERE id = ? AND product_id = ?", (image_id, product_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Image not found")
        conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
        repo.renumber_images(conn, product_id, [r["id"] for r in repo.admin_images(conn, product_id)])
        repo.touch(conn, product_id)
    delete_stored_image(settings.uploads_dir, row["path"])
    return _get_or_404(conn, product_id)


@router.post(
    "/products/{product_id}/images",
    response_model=AdminProduct,
    status_code=201,
    dependencies=[Depends(limit("upload"))],
)
async def upload_image(
    product_id: ProductIdPath,
    request: Request,
    conn: Db,
    settings: Annotated[Settings, Depends(get_settings)],
):
    """multipart/form-data with a single "file" field. The body is parsed here,
    after rate limiting and auth, and only once its declared size is acceptable."""
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length required")
    if int(length) > MAX_UPLOAD_REQUEST:
        raise HTTPException(status_code=413, detail="File too large (max 5 MB)")

    def check_capacity() -> None:
        _get_or_404(conn, product_id)
        if len(repo.admin_images(conn, product_id)) >= MAX_IMAGES:
            raise HTTPException(status_code=409, detail=f"At most {MAX_IMAGES} images per product")

    await run_in_threadpool(check_capacity)

    form = await request.form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=422, detail='Expected a file in field "file"')
        data = await upload.read(MAX_IMAGE_BYTES + 1)
    finally:
        await form.close()

    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 5 MB)")
    ext = detect_image_type(data)
    if ext is None:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG and WebP images are allowed")

    def store() -> AdminProduct:
        check_capacity()
        path = save_image(settings.uploads_dir, data, ext)
        try:
            with conn:
                position = conn.execute(
                    "SELECT COALESCE(MAX(position) + 1, 0) FROM product_images WHERE product_id = ?",
                    (product_id,),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO product_images (product_id, path, position) VALUES (?, ?, ?)",
                    (product_id, path, position),
                )
                repo.touch(conn, product_id)
        except Exception:
            delete_stored_image(settings.uploads_dir, path)
            raise
        return _get_or_404(conn, product_id)

    return await run_in_threadpool(store)


# --- orders -------------------------------------------------------------------

OrderIdPath = Annotated[int, Path(ge=1, le=2**53)]


def _order_or_404(conn: sqlite3.Connection, order_id: int) -> AdminOrder:
    order = orders.admin_get(conn, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/orders", response_model=AdminOrderList)
def list_orders(
    conn: Db,
    response: Response,
    status: OrderStatus | None = None,
    limit_: Annotated[int, Query(alias="limit", ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    response.headers["Cache-Control"] = "no-store"
    items, total = orders.admin_list(conn, status, limit_, offset)
    return AdminOrderList(items=items, total=total)


@router.get("/orders/{order_id}", response_model=AdminOrder)
def get_order(order_id: OrderIdPath, conn: Db, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _order_or_404(conn, order_id)


@router.patch("/orders/{order_id}", response_model=AdminOrder)
def update_order(order_id: OrderIdPath, body: OrderStatusUpdate, conn: Db):
    with conn:
        if not orders.set_status(conn, order_id, body.status):
            raise HTTPException(status_code=404, detail="Order not found")
    return _order_or_404(conn, order_id)


@router.post("/orders/{order_id}/resend", response_model=AdminOrder)
def resend_order_notification(
    order_id: OrderIdPath, conn: Db, request: Request, background: BackgroundTasks
):
    """Queues the notification again and sends it right after the response."""
    with conn:
        _order_or_404(conn, order_id)
        notify.reset_for_resend(conn, order_id)
    background.add_task(notify.run_due, request.app, order_id)
    return _order_or_404(conn, order_id)
