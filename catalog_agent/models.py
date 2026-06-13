from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Category:
    name: str
    url: str


@dataclass(slots=True)
class CategorySearchConfig:
    application_id: str
    api_key: str
    index_name: str
    category_path: str
    category_level: int
    category_id: str
    source_url: str

    @property
    def product_index_name(self) -> str:
        suffix = "_products"
        return (
            self.index_name
            if self.index_name.endswith(suffix)
            else f"{self.index_name}{suffix}"
        )


@dataclass(slots=True)
class Product:
    source: str
    source_product_id: str | None
    parent_sku: str | None
    sku: str
    product_name: str
    brand: str | None
    manufacturer_part_number: str | None
    category_hierarchy: list[str]
    product_url: str
    price: float | None
    currency: str | None
    unit_pack_size: str | None
    availability: str | None
    description: str | None
    specifications: dict[str, Any] = field(default_factory=dict)
    image_urls: list[str] = field(default_factory=list)
    alternative_products: list[dict[str, str]] = field(default_factory=list)
    scraped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

