from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from catalog_agent.models import Product

LOGGER = logging.getLogger(__name__)


class CrawlStore:
    @classmethod
    def has_resumable_state(cls, output_dir: Path) -> bool:
        """Return whether an earlier crawl left checkpointed or raw state."""
        state_dir = output_dir / "state"
        return any(
            path.is_file() and path.stat().st_size > 0
            for path in (
                state_dir / "checkpoint.json",
                state_dir / "products.jsonl",
            )
        )

    def __init__(self, output_dir: Path, *, resume: bool) -> None:
        self.output_dir = output_dir
        self.state_dir = output_dir / "state"
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.raw_path = self.state_dir / "products.jsonl"
        self.failures_path = output_dir / "failures.jsonl"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not resume:
            for path in (
                self.checkpoint_path,
                self.raw_path,
                self.failures_path,
            ):
                path.unlink(missing_ok=True)
        self.completed_urls = self._load_checkpoint()

    def is_completed(self, url: str) -> bool:
        return url in self.completed_urls

    def save_products(self, products: list[Product]) -> None:
        with self.raw_path.open("a", encoding="utf-8") as handle:
            for product in products:
                handle.write(
                    json.dumps(
                        product.to_dict(), ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )

    def mark_completed(self, url: str) -> None:
        self.completed_urls.add(url)
        self._atomic_json(
            self.checkpoint_path,
            {"completed_urls": sorted(self.completed_urls)},
        )

    def record_failure(
        self, *, url: str, stage: str, error: str
    ) -> None:
        with self.failures_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"url": url, "stage": stage, "error": error},
                    ensure_ascii=False,
                )
                + "\n"
            )

    def export(
        self, products: list[dict[str, Any]] | None = None
    ) -> tuple[Path, Path, int]:
        if products is None:
            products = self.load_products()
        json_path = self.output_dir / "products.json"
        csv_path = self.output_dir / "products.csv"
        self._atomic_json(json_path, products)
        self._write_csv(csv_path, products)
        return json_path, csv_path, len(products)

    def _load_checkpoint(self) -> set[str]:
        if not self.checkpoint_path.exists():
            return set()
        data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        return set(data.get("completed_urls", []))

    def load_products(self) -> list[dict[str, Any]]:
        """Load append-only working state and retain the latest row per SKU."""
        if not self.raw_path.exists():
            return []
        by_sku: dict[str, dict[str, Any]] = {}
        lines = self.raw_path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                product = json.loads(line)
            except json.JSONDecodeError:
                if index != len(lines) - 1:
                    raise
                LOGGER.warning(
                    "Ignoring incomplete final JSONL row from interrupted write",
                    extra={"path": str(self.raw_path)},
                )
                continue
            by_sku[product["sku"]] = product
        return [by_sku[sku] for sku in sorted(by_sku)]

    @staticmethod
    def _write_csv(path: Path, products: list[dict[str, Any]]) -> None:
        if not products:
            path.write_text("", encoding="utf-8")
            return
        fieldnames = list(products[0])
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for product in products:
                row = {
                    key: (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (list, dict))
                        else value
                    )
                    for key, value in product.items()
                }
                writer.writerow(row)

    @staticmethod
    def _atomic_json(path: Path, data: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
