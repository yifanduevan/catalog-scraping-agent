from __future__ import annotations

from dataclasses import dataclass

from catalog_agent.models import Product
from catalog_agent.parsing import normalize_whitespace


@dataclass(slots=True)
class ValidationResult:
    products: list[Product]
    errors: list[str]


class ValidatorAgent:
    """Normalizes required fields and deduplicates by SKU."""

    def validate(self, products: list[Product]) -> ValidationResult:
        accepted: list[Product] = []
        errors: list[str] = []
        seen_skus: set[str] = set()

        for product in products:
            product.sku = normalize_whitespace(product.sku)
            product.product_name = normalize_whitespace(product.product_name)
            product.product_url = product.product_url.strip()

            missing = [
                name
                for name, value in (
                    ("sku", product.sku),
                    ("product_name", product.product_name),
                    ("product_url", product.product_url),
                )
                if not value
            ]
            if missing:
                errors.append(
                    f"{product.product_url or '<unknown>'}: missing "
                    + ", ".join(missing)
                )
                continue
            if product.sku in seen_skus:
                continue
            seen_skus.add(product.sku)
            accepted.append(product)

        return ValidationResult(products=accepted, errors=errors)

