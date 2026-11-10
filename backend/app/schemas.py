"""Pydantic models and shared field constraints.

Public response models must never contain price_supplier, price_reference or
internal fields (edited_by_admin, needs_review, review_reasons, source_*,
status). Keep admin and public models separate.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATEGORIES = ("Взуття", "Одяг")
STATUSES = ("draft", "published", "hidden")

Category = Literal["Взуття", "Одяг"]
Status = Literal["draft", "published", "hidden"]

PRODUCT_ID_RE = r"^[a-z0-9][a-z0-9-]{0,63}$"
# Relative image paths only: no scheme, no "..", no leading slash.
IMAGE_PATH_RE = re.compile(
    r"^(?:public/images/products|images/products|uploads/products)/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.(?:jpg|jpeg|png|webp)$"
)

MAX_SIZES = 20
MAX_IMAGES = 10

ProductId = Annotated[str, Field(pattern=PRODUCT_ID_RE)]
Title = Annotated[str, Field(min_length=1, max_length=120)]
Description = Annotated[str, Field(max_length=2000)]
Price = Annotated[int, Field(ge=0, le=10_000_000)]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z_]{0,39}$")]
MAX_REASONS = 10


def normalize_sizes(value: list[str | int]) -> list[str]:
    """Sizes are stored as strings ("42", "XL"); duplicates are dropped."""
    if len(value) > MAX_SIZES:
        raise ValueError(f"at most {MAX_SIZES} sizes")
    result: list[str] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("size must be a string or integer")
        size = str(item).strip()
        if not 1 <= len(size) <= 10:
            raise ValueError("each size must be 1..10 characters")
        if size not in result:
            result.append(size)
    return result


def validate_image_path(path: str) -> str:
    if not IMAGE_PATH_RE.match(path) or ".." in path:
        raise ValueError("invalid image path")
    return path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ImportRecord(StrictModel):
    """One item of products.public.json (format defined for import-json)."""

    id: ProductId
    title: Title
    description: Description = ""
    category: Category
    price_retail: Price
    sizes: list[str | int] = Field(default_factory=list)
    in_stock: bool = True
    popular: bool = False
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    source_channel: Annotated[str, Field(max_length=100)] | None = None
    source_message_id: Annotated[int, Field(ge=0)] | None = None
    needs_review: bool = False
    review_reasons: list[ReasonCode] = Field(default_factory=list, max_length=MAX_REASONS)
    # Admin-only: id of the main product of a possible-duplicate group.
    duplicate_group: ProductId | None = None

    _sizes = field_validator("sizes")(normalize_sizes)

    @field_validator("images")
    @classmethod
    def _images(cls, value: list[str]) -> list[str]:
        return [validate_image_path(p) for p in value]


class CostEntry(StrictModel):
    """One value of import/private/costs.json: {"<product id>": {...}}."""

    price_supplier: Price | None = None
    price_reference: Price | None = None  # supplier's suggested retail, internal only


class PublicProduct(BaseModel):
    """Storefront view. Field names match what the storefront JS already uses.

    Whitelist only: never add price_supplier, status, edited_by_admin or
    source_* here. Built field by field in repo.to_public, never from **row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    category: str
    price: int
    sizes: list[str]
    inStock: bool
    popular: bool
    imageUrl: str | None
    images: list[str]


# --- admin models (may contain price_supplier and internal fields) ---------


class ProductCreate(StrictModel):
    title: Title
    description: Description = ""
    category: Category
    price_retail: Price
    price_supplier: Price | None = None
    sizes: list[str | int] = Field(default_factory=list)
    in_stock: bool = True
    popular: bool = False
    status: Status = "draft"

    _sizes = field_validator("sizes")(normalize_sizes)


class ProductUpdate(StrictModel):
    """Partial update: only fields present in the body are changed.
    price_supplier may be set to null; other fields may not."""

    title: Title | None = None
    description: Description | None = None
    category: Category | None = None
    price_retail: Price | None = None
    price_supplier: Price | None = None
    sizes: list[str | int] | None = None
    in_stock: bool | None = None
    popular: bool | None = None
    status: Status | None = None
    # Explicit "reviewed" confirmation from the admin; only false is accepted.
    needs_review: Literal[False] | None = None

    @field_validator("sizes")
    @classmethod
    def _sizes(cls, value):
        return None if value is None else normalize_sizes(value)

    @model_validator(mode="after")
    def _no_null_required(self):
        for name in self.model_fields_set - {"price_supplier"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class BulkStatus(StrictModel):
    ids: list[ProductId] = Field(min_length=1, max_length=200)
    status: Status


class ImageOrder(StrictModel):
    image_ids: list[int] = Field(max_length=MAX_IMAGES)


class AdminImage(BaseModel):
    id: int
    url: str
    position: int


class DuplicateMember(BaseModel):
    """Another product of the same possible-duplicate group (admin only)."""

    id: str
    title: str
    status: str
    is_main: bool
    image_url: str | None
    image_count: int


class AdminProduct(BaseModel):
    id: str
    title: str
    description: str
    category: str
    price_retail: int
    price_supplier: int | None
    price_reference: int | None
    sizes: list[str]
    in_stock: bool
    popular: bool
    status: str
    edited_by_admin: bool
    needs_review: bool
    review_reasons: list[str]
    source_channel: str | None
    source_message_id: int | None
    created_at: str
    updated_at: str
    images: list[AdminImage]
    # Possible-duplicate group key (= id of the group's main product), also set
    # on the main product itself while other products point to it.
    duplicate_group: str | None = None
    duplicates: list[DuplicateMember] = []  # filled for a single product only


class AdminProductList(BaseModel):
    items: list[AdminProduct]
    total: int
