from __future__ import annotations

import html
import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from catalog_agent.models import Product
from catalog_agent.parsing import normalize_whitespace

LOGGER = logging.getLogger(__name__)

PACK_SIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "pack_size": {"type": ["string", "null"]},
        "evidence": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["pack_size", "evidence", "confidence"],
    "additionalProperties": False,
}

PRODUCT_TAGS_SCHEMA = {
    "type": "object",
    "properties": {
        "product_type": {"type": "string"},
        "attributes": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
    },
    "required": ["product_type", "attributes"],
    "additionalProperties": False,
}


class StructuredOutputClient(Protocol):
    def generate(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        input_text: str,
    ) -> dict[str, Any]: ...


class OpenAIResponsesClient:
    """Minimal standard-library client for OpenAI structured Responses output."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.4-mini",
        timeout_seconds: float = 30,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for AI enrichment")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def generate(
        self,
        *,
        schema_name: str,
        schema: dict[str, Any],
        system_prompt: str,
        input_text: str,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_text},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        response = self._request(request)
        output_text = self._output_text(response)
        return json.loads(output_text)

    def _request(self, request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(
                    f"OpenAI API returned HTTP {exc.code}: {body[:500]}"
                )
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc

            if attempt < self.max_retries:
                time.sleep((2**attempt) + random.uniform(0, 0.25))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return str(content["text"])
                if content.get("type") == "refusal":
                    raise RuntimeError(
                        f"OpenAI refused enrichment: {content.get('refusal')}"
                    )
        raise RuntimeError("OpenAI response did not contain structured output")


@dataclass(slots=True)
class EnrichmentResult:
    products: list[Product]
    errors: list[str]


class AIEnrichmentAgent:
    """Add evidence-backed pack sizes and normalized matching tags."""

    def __init__(
        self,
        client: StructuredOutputClient,
        *,
        pack_confidence_threshold: float = 0.65,
    ) -> None:
        self.client = client
        self.pack_confidence_threshold = pack_confidence_threshold

    def enrich(self, products: list[Product]) -> EnrichmentResult:
        errors: list[str] = []
        for product in products:
            if not product.unit_pack_size:
                try:
                    self._enrich_pack_size(product)
                except Exception as exc:
                    LOGGER.warning(
                        "AI pack-size enrichment failed",
                        extra={"sku": product.sku, "error": str(exc)},
                    )
                    errors.append(f"{product.sku}: pack size: {exc}")

            try:
                self._enrich_tags(product)
            except Exception as exc:
                LOGGER.warning(
                    "AI tag enrichment failed",
                    extra={"sku": product.sku, "error": str(exc)},
                )
                errors.append(f"{product.sku}: tags: {exc}")

        return EnrichmentResult(products=products, errors=errors)

    def _enrich_pack_size(self, product: Product) -> None:
        source_text = self._source_text(product)
        result = self.client.generate(
            schema_name="pack_size_extraction",
            schema=PACK_SIZE_SCHEMA,
            system_prompt=(
                "Extract a product's purchasable unit or pack size only when it "
                "is explicitly supported by the supplied text. Handle prose "
                "such as 'ten sleeves with six items each' by returning the "
                "normalized total and unit. Do not treat physical dimensions "
                "as pack size. Return null values when evidence is insufficient."
            ),
            input_text=source_text,
        )
        pack_size = normalize_whitespace(result.get("pack_size"))
        evidence = normalize_whitespace(result.get("evidence"))
        confidence = float(result.get("confidence", 0))
        if not pack_size or not evidence:
            return
        if confidence < self.pack_confidence_threshold:
            return
        if not self._pack_evidence_supported(
            pack_size=pack_size,
            evidence=evidence,
            source_text=source_text,
        ):
            raise ValueError(
                "pack-size evidence is not sufficiently supported by source "
                f"text (pack_size={pack_size!r}, evidence={evidence!r})"
            )

        product.unit_pack_size = pack_size
        product.unit_pack_size_source = "llm"
        product.specifications["pack_size_evidence"] = evidence
        product.specifications["pack_size_confidence"] = round(confidence, 3)

    def _enrich_tags(self, product: Product) -> None:
        context = {
            "name": product.product_name,
            "category_hierarchy": product.category_hierarchy,
            "description": product.description,
            "variant": product.specifications.get("variant"),
            "specifications": product.specifications,
        }
        result = self.client.generate(
            schema_name="product_matching_tags",
            schema=PRODUCT_TAGS_SCHEMA,
            system_prompt=(
                "Normalize a dental catalog product for similarity matching. "
                "Return one concise lowercase product_type and up to 12 concise "
                "lowercase attributes explicitly supported by the input, such "
                "as material, intended use, form, sterility, or powder-free. "
                "Exclude brand, SKU, price, availability, dimensions, and pack "
                "counts. Do not invent properties."
            ),
            input_text=json.dumps(context, ensure_ascii=False, sort_keys=True),
        )
        values = [result.get("product_type"), *result.get("attributes", [])]
        product.normalized_tags = self._normalize_tags(values)

    @staticmethod
    def _source_text(product: Product) -> str:
        values = [
            product.product_name,
            product.description or "",
            str(product.specifications.get("variant", "")),
        ]
        return "\n".join(value for value in values if value)

    @classmethod
    def _pack_evidence_supported(
        cls,
        *,
        pack_size: str,
        evidence: str,
        source_text: str,
    ) -> bool:
        """Allow formatting differences while requiring a supported pack claim."""
        normalized_evidence = cls._normalize_match_text(evidence)
        normalized_source = cls._normalize_match_text(source_text)
        normalized_pack_size = cls._normalize_match_text(pack_size)
        pack_claims = cls._pack_claims(pack_size)
        evidence_claims = cls._pack_claims(evidence)
        source_claims = cls._pack_claims(source_text)

        # Exact source evidence is accepted when the normalized pack result
        # agrees with it. This covers punctuation differences while avoiding a
        # number-only match against a model number or dimension.
        if (
            normalized_evidence
            and normalized_evidence in normalized_source
        ):
            if pack_claims & evidence_claims & source_claims:
                return True
            if cls._is_verbal_pack_evidence(evidence):
                return True
            if (
                cls._is_pack_descriptor(pack_size)
                and normalized_pack_size in normalized_evidence
            ):
                return True

        # Treat aliases and common wording as equivalent: "12/bx" equals
        # "12/box", "50/cs" equals "50/case", and "0.5cc" equals "0.5 ml".
        return bool(pack_claims & evidence_claims & source_claims)

    @staticmethod
    def _normalize_match_text(value: str) -> str:
        value = re.sub(
            r"(?<!\d)\.(?=\d)", "0.", html.unescape(value).casefold()
        )
        aliases = {
            "bags": "bag",
            "boxes": "box",
            "bx": "box",
            "packs": "pack",
            "cases": "case",
            "cs": "case",
            "pkgs": "package",
            "pkg": "package",
            "packages": "package",
            "pairs": "pair",
            "pieces": "piece",
            "pcs": "piece",
            "pc": "piece",
            "items": "item",
            "gloves": "glove",
            "counts": "count",
            "ct": "count",
            "sleeves": "sleeve",
            "bottles": "bottle",
            "jars": "jar",
            "syringes": "syringe",
            "kits": "kit",
            "vials": "vial",
            "tubes": "tube",
            "cc": "ml",
        }
        tokens = re.findall(r"\d+(?:\.\d+)?|[a-z]+", value)
        return " ".join(aliases.get(token, token) for token in tokens)

    @classmethod
    def _pack_claims(cls, value: str) -> set[tuple[str, ...]]:
        normalized = cls._normalize_match_text(value)
        # Remove phrases that contain pack-like numbers but describe product
        # capacity or operating specifications rather than the sold quantity.
        normalized = re.sub(
            r"\bhold(?:s|ing)? \d+(?:\.\d+)? "
            r"(?:bag|box|pack|case|package|pair|piece|item|glove)\b",
            " ",
            normalized,
        )
        normalized = re.sub(
            r"\b\d+(?:\.\d+)? ml priming volume\b",
            " ",
            normalized,
        )
        normalized = re.sub(
            r"\b\d+(?:\.\d+)? drops? (?:per )?ml\b",
            " ",
            normalized,
        )

        claims: set[tuple[str, ...]] = set()
        containers = (
            r"bag|box|pack|case|package|sleeve|bottle|jar|"
            r"syringe|kit|vial|tube"
        )
        inner_units = r"pair|piece|item|glove|bag|box|syringe|vial|tube"
        patterns = (
            rf"\b(?P<quantity>\d+)(?: (?P<inner>{inner_units}))?"
            rf"(?: per)? (?P<unit>{containers})\b",
            rf"\b(?P<unit>{containers}) of (?P<quantity>\d+)\b",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, normalized):
                claims.add(
                    ("count", match.group("quantity"), match.group("unit"))
                )

        # "16 bags/sleeve" should agree with "sleeve of 16": the outer
        # container is the meaningful packaging unit for this claim.
        outer_pattern = (
            rf"\b(?P<quantity>\d+) (?:{inner_units}) "
            rf"(?P<unit>{containers})\b"
        )
        for match in re.finditer(outer_pattern, normalized):
            claims.add(
                ("count", match.group("quantity"), match.group("unit"))
            )

        measurement_pattern = (
            r"\b(?P<quantity>\d+(?:\.\d+)?) "
            r"(?:(?:fl|fluid) )?(?P<unit>ml|oz|g|mg)\b"
        )
        for match in re.finditer(measurement_pattern, normalized):
            claims.add(
                (
                    "measure",
                    cls._normalize_quantity(match.group("quantity")),
                    match.group("unit"),
                )
            )
        return claims

    @staticmethod
    def _normalize_quantity(value: str) -> str:
        try:
            return format(Decimal(value).normalize(), "f")
        except InvalidOperation:
            return value

    @classmethod
    def _is_pack_descriptor(cls, value: str) -> bool:
        normalized = cls._normalize_match_text(value)
        return bool(
            re.fullmatch(
                r"(?:standard|starter|refill|multiuse) "
                r"(?:package|pack|kit)",
                normalized,
            )
        )

    @classmethod
    def _is_verbal_pack_evidence(cls, value: str) -> bool:
        normalized = cls._normalize_match_text(value)
        number_words = (
            r"one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve"
        )
        count_units = (
            r"bag|box|pack|case|package|sleeve|pair|piece|item|"
            r"glove|bottle|jar|syringe|kit|vial|tube"
        )
        return bool(
            re.search(rf"\b(?:{number_words}) (?:{count_units})\b", normalized)
            and re.search(r"\beach\b", normalized)
        )

    @staticmethod
    def _normalize_tags(values: list[Any]) -> list[str]:
        tags: list[str] = []
        for value in values:
            tag = normalize_whitespace(str(value)).casefold()
            if tag and tag not in tags:
                tags.append(tag)
        return tags
