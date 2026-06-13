from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser
from typing import Any

from catalog_agent.models import CategorySearchConfig


class ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._script_type: str | None = None
        self._buffer: list[str] = []
        self.json_ld_scripts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        self._script_type = attributes.get("type")
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._script_type is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._script_type is None:
            return
        if self._script_type.lower() == "application/ld+json":
            self.json_ld_scripts.append("".join(self._buffer))
        self._script_type = None
        self._buffer = []


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return normalize_whitespace(" ".join(self.parts))


def normalize_whitespace(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str | None) -> str | None:
    if not value:
        return None
    parser = TextExtractor()
    parser.feed(html.unescape(value))
    text = parser.text()
    return text or None


def extract_json_assignment(document: str, marker: str) -> Any:
    start = document.find(marker)
    if start < 0:
        raise ValueError(f"Could not find JavaScript marker: {marker}")
    start += len(marker)
    while start < len(document) and document[start].isspace():
        start += 1
    value, _ = json.JSONDecoder().raw_decode(document[start:])
    return value


def parse_algolia_config(
    document: str, source_url: str
) -> CategorySearchConfig:
    raw = extract_json_assignment(document, "window.algoliaConfig =")
    request = raw["request"]
    if not raw.get("isCategoryPage") or not request.get("path"):
        raise ValueError(f"URL is not a live category page: {source_url}")
    return CategorySearchConfig(
        application_id=raw["applicationId"],
        api_key=raw["apiKey"],
        index_name=raw["indexName"],
        category_path=request["path"],
        category_level=int(request["level"]),
        category_id=str(request["categoryId"]),
        source_url=source_url,
    )


def parse_json_ld(document: str) -> list[dict[str, Any]]:
    collector = ScriptCollector()
    collector.feed(document)
    records: list[dict[str, Any]] = []
    for script in collector.json_ld_scripts:
        try:
            value = json.loads(script)
        except json.JSONDecodeError:
            continue
        candidates = value if isinstance(value, list) else [value]
        records.extend(item for item in candidates if isinstance(item, dict))
    return records


def find_json_ld(
    records: list[dict[str, Any]], schema_type: str
) -> dict[str, Any] | None:
    for record in records:
        if record.get("@type") == schema_type:
            return record
        graph = record.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict) and item.get("@type") == schema_type:
                    return item
    return None


def parse_master_data(document: str) -> dict[str, dict[str, Any]]:
    try:
        encoded = extract_json_assignment(document, "window.masterData =")
    except ValueError:
        return {}
    if not isinstance(encoded, str):
        return {}
    decoded = json.loads(encoded)
    return decoded if isinstance(decoded, dict) else {}


def parse_initial_images(document: str) -> list[str]:
    try:
        images = extract_json_assignment(document, "initialImages:")
    except ValueError:
        return []
    if not isinstance(images, list):
        return []
    urls: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("full") or image.get("img")
        if url and url not in urls:
            urls.append(url)
    return urls


def parse_price_from_html(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("product_price", "price", "lowPrice", "default", "USD"):
            if key in value:
                parsed = parse_price_from_html(value[key])
                if parsed is not None:
                    return parsed
        if len(value) == 1:
            return parse_price_from_html(next(iter(value.values())))
        return None
    if not isinstance(value, str):
        return None
    amount = re.search(r'data-price-amount=["\']([0-9.]+)', value)
    if not amount:
        amount = re.search(r"\$([0-9,.]+)", value)
    if not amount:
        amount = re.fullmatch(r"\s*([0-9.]+)\s*", value)
    return float(amount.group(1).replace(",", "")) if amount else None


def parse_pack_size(description: str | None) -> str | None:
    if not description:
        return None
    patterns = [
        r"\b\d[\d,]*\s*(?:gloves?|pieces?|pcs?)?\s*(?:/|per\s+)"
        r"(?:box|pack|case|pkg|package)\b",
        r"\b(?:box|pack|case|pkg|package)\s+of\s+\d[\d,]*\b",
        r"\b\d[\d,]*\s*(?:ct|count)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, description, flags=re.IGNORECASE)
        if match:
            return normalize_whitespace(match.group(0))
    return None


def parse_description_specs(description: str | None) -> dict[str, str]:
    if not description:
        return {}
    specs: dict[str, str] = {}
    for line in re.split(r"[\r\n]+", html.unescape(description)):
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = normalize_whitespace(key)
        value = normalize_whitespace(value)
        if 0 < len(key) <= 60 and value:
            specs[key] = value
    return specs
