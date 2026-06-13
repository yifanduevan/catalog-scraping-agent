from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from catalog_agent.http import HttpClient
from catalog_agent.models import Product
from catalog_agent.parsing import (
    find_json_ld,
    normalize_whitespace,
    parse_description_specs,
    parse_initial_images,
    parse_json_ld,
    parse_master_data,
    parse_pack_size,
    parse_price_from_html,
    strip_html,
)

LOGGER = logging.getLogger(__name__)


class ExtractorAgent:
    """Turn one product-family page into normalized SKU-level products.

    The Algolia listing is useful for discovery but is not the final catalog
    record. This agent enriches it from the detail page using:

    - Product JSON-LD for family name, description, brand, price, and images.
    - BreadcrumbList JSON-LD for the category hierarchy.
    - Safco's ``window.masterData`` for purchasable child variations.

    A simple family returns one Product. A grouped family returns one Product
    per child SKU.
    """

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    def extract(
        self, product_url: str, listing: dict[str, Any]
    ) -> list[Product]:
        """Download and normalize one product-family detail page."""
        LOGGER.info("Extracting product", extra={"url": product_url})
        document = self.client.get_text(product_url)

        # JSON-LD is standards-based structured data included for search engines
        # and is more stable than scraping presentation-oriented CSS selectors.
        records = parse_json_ld(document)
        product_ld = find_json_ld(records, "Product") or {}
        breadcrumb_ld = find_json_ld(records, "BreadcrumbList") or {}

        # masterData is Safco-specific embedded JSON. On grouped pages it maps
        # each purchasable child SKU to price, stock, size/pack, and MPN data.
        master_data = parse_master_data(document)

        # Family-level values are computed once, then reused by every variation.
        page_images = self._page_images(product_ld, document, listing)
        hierarchy = self._category_hierarchy(breadcrumb_ld, listing)
        scraped_at = datetime.now(UTC).isoformat()

        if master_data:
            # Example: one "Aquasoft" family page expands into x-small, small,
            # medium, large, and x-large SKU rows.
            return [
                self._from_variant(
                    variant,
                    listing=listing,
                    product_ld=product_ld,
                    product_url=product_url,
                    page_images=page_images,
                    hierarchy=hierarchy,
                    scraped_at=scraped_at,
                )
                for variant in master_data.values()
            ]

        # No child variation map means this page represents one standalone SKU.
        return [
            self._from_parent(
                listing=listing,
                product_ld=product_ld,
                product_url=product_url,
                page_images=page_images,
                hierarchy=hierarchy,
                scraped_at=scraped_at,
            )
        ]

    def _from_variant(
        self,
        variant: dict[str, Any],
        *,
        listing: dict[str, Any],
        product_ld: dict[str, Any],
        product_url: str,
        page_images: list[str],
        hierarchy: list[str],
        scraped_at: str,
    ) -> Product:
        """Merge one child variation with its shared family-level metadata."""
        # JSON-LD normally contains the grouped/parent SKU. If absent, use the
        # first SKU supplied by the discovery record.
        parent_sku = self._string(product_ld.get("sku")) or self._first_sku(
            listing
        )

        # The variant description commonly contains the discriminating values,
        # such as "Medium, 300/box"; the family description explains the item.
        variant_description = strip_html(
            variant.get("description") or variant.get("short_description")
        )
        description = strip_html(product_ld.get("description"))

        # Convert "Label: value" lines into a dictionary and retain the complete
        # variant description even when it is not a labeled specification.
        specifications = parse_description_specs(product_ld.get("description"))
        if variant_description:
            specifications["variant"] = variant_description
        hazard = variant.get("hazard_type")
        if hazard:
            specifications["hazard_type"] = hazard

        pack_size = parse_pack_size(variant_description)

        # Prefer variation-specific images, then add family images, remove
        # duplicates, and discard known placeholder URLs.
        images = self._unique_urls(
            [
                variant.get("main_image"),
                variant.get("image"),
                *page_images,
            ]
        )
        return Product(
            source="safcodental.com",
            source_product_id=self._string(variant.get("id")),
            parent_sku=parent_sku,
            sku=self._string(variant.get("sku")) or "",
            # Prefer the child name because it includes size/pack information.
            product_name=normalize_whitespace(
                variant.get("name") or product_ld.get("name")
            ),
            brand=self._brand(product_ld, listing, variant),
            manufacturer_part_number=self._string(
                variant.get("manufacturer_part_number")
            ),
            category_hierarchy=hierarchy,
            product_url=product_url,
            price=parse_price_from_html(
                variant.get("product_price") or variant.get("price")
            ),
            currency=self._currency(product_ld, listing),
            unit_pack_size=pack_size,
            availability=normalize_whitespace(
                variant.get("stock_availability_label")
                or variant.get("stock_availability")
            )
            or None,
            description=description,
            unit_pack_size_source="rule" if pack_size else None,
            specifications=specifications,
            image_urls=images,
            alternative_products=[],
            scraped_at=scraped_at,
        )

    def _from_parent(
        self,
        *,
        listing: dict[str, Any],
        product_ld: dict[str, Any],
        product_url: str,
        page_images: list[str],
            hierarchy: list[str],
        scraped_at: str,
    ) -> Product:
        """Create one SKU row when the page has no grouped child variations."""
        description = strip_html(product_ld.get("description"))
        offer = product_ld.get("offers")

        # Some JSON-LD pages omit offers or use an unexpected shape. Treat that
        # as missing data and fall back to the Algolia listing below.
        if not isinstance(offer, dict):
            offer = {}
        pack_size = parse_pack_size(description)
        return Product(
            source="safcodental.com",
            source_product_id=self._string(listing.get("objectID")),
            parent_sku=None,
            # Prefer page JSON-LD, then use the discovery record as fallback.
            sku=self._string(product_ld.get("sku"))
            or self._first_sku(listing)
            or "",
            product_name=normalize_whitespace(
                product_ld.get("name") or listing.get("name")
            ),
            brand=self._brand(product_ld, listing, {}),
            manufacturer_part_number=self._string(
                listing.get("manufacturer_part_number")
            ),
            category_hierarchy=hierarchy,
            product_url=product_url,
            price=parse_price_from_html(
                offer.get("price")
                or offer.get("lowPrice")
                or listing.get("price")
            ),
            currency=self._currency(product_ld, listing),
            unit_pack_size=pack_size,
            availability=self._availability(
                offer.get("availability")
                or listing.get("stock_status_label")
                or listing.get("stock_availability")
            ),
            description=description,
            unit_pack_size_source="rule" if pack_size else None,
            specifications=parse_description_specs(
                product_ld.get("description")
            ),
            image_urls=page_images,
            alternative_products=[],
            scraped_at=scraped_at,
        )

    @staticmethod
    def _page_images(
        product_ld: dict[str, Any],
        document: str,
        listing: dict[str, Any],
    ) -> list[str]:
        """Combine image sources ordered from richest to weakest fallback."""
        images = product_ld.get("image", [])
        if isinstance(images, str):
            images = [images]
        return ExtractorAgent._unique_urls(
            [
                *images,
                *parse_initial_images(document),
                listing.get("image_url"),
            ]
        )

    @staticmethod
    def _category_hierarchy(
        breadcrumb_ld: dict[str, Any], listing: dict[str, Any]
    ) -> list[str]:
        """Prefer product breadcrumbs, falling back to the category search path."""
        items = breadcrumb_ld.get("itemListElement", [])
        names = [
            normalize_whitespace(item.get("name"))
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]
        if len(names) >= 3:
            # Remove "Home" at the start and the product name at the end.
            return names[1:-1]
        path = listing.get("_category_path", "")
        return [part.strip() for part in path.split("///") if part.strip()]

    @staticmethod
    def _brand(
        product_ld: dict[str, Any],
        listing: dict[str, Any],
        variant: dict[str, Any],
    ) -> str | None:
        """Choose the most SKU-specific available manufacturer/brand value."""
        brand = product_ld.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        return (
            normalize_whitespace(
                variant.get("manufacturer_name")
                or brand
                or listing.get("manufacturer_name")
            )
            or None
        )

    @staticmethod
    def _currency(
        product_ld: dict[str, Any], listing: dict[str, Any]
    ) -> str | None:
        """Read explicit JSON-LD currency, or infer USD from Safco listing price."""
        offer = product_ld.get("offers")
        if isinstance(offer, dict) and offer.get("priceCurrency"):
            return str(offer["priceCurrency"])
        if listing.get("price"):
            return "USD"
        return None

    @staticmethod
    def _availability(value: Any) -> str | None:
        """Convert schema.org URLs and raw labels to human-readable stock text."""
        if not value:
            return None
        text = str(value).rsplit("/", 1)[-1]
        mapping = {
            "InStock": "In stock",
            "OutOfStock": "Out of stock",
            "PreOrder": "Preorder",
        }
        return mapping.get(text, normalize_whitespace(text))

    @staticmethod
    def _first_sku(listing: dict[str, Any]) -> str | None:
        """Handle Algolia's SKU field, which may be a scalar or a list."""
        sku = listing.get("sku")
        if isinstance(sku, list):
            return str(sku[0]) if sku else None
        return str(sku) if sku else None

    @staticmethod
    def _string(value: Any) -> str | None:
        """Normalize optional IDs/codes without converting missing values to text."""
        if value is None or value is False:
            return None
        return normalize_whitespace(str(value)) or None

    @staticmethod
    def _unique_urls(values: list[Any]) -> list[str]:
        """Remove non-URLs, known placeholders, and duplicate image entries."""
        result: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            if "placeholder" in value.lower():
                continue
            if value and value not in result:
                result.append(value)
        return result
