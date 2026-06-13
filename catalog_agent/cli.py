from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from catalog_agent.http import HttpClient
from catalog_agent.models import Category
from catalog_agent.orchestrator import CatalogScrapingAgent
from catalog_agent.storage import CrawlStore


def build_parser() -> argparse.ArgumentParser:
    """Describe the command-line interface and convert arguments to Python types.

    This module is the composition root of the application: it receives user
    input, creates the concrete infrastructure objects, and hands control to
    the orchestrator. Scraping logic intentionally lives elsewhere.
    """
    parser = argparse.ArgumentParser(
        prog="catalog-agent",
        description="Scrape normalized Safco Dental product catalog data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl = subparsers.add_parser("crawl", help="Crawl configured categories")

    # Category seeds are kept outside the code so a reviewer can change the
    # crawl scope without modifying the navigator or orchestrator.
    crawl.add_argument(
        "--config",
        type=Path,
        default=Path("config/categories.json"),
        help="Category configuration JSON",
    )
    crawl.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/sample"),
        help="Output and checkpoint directory",
    )

    # This limit counts unique product-family pages, not final SKU rows. One
    # family page can expand into several variations in ExtractorAgent.
    crawl.add_argument(
        "--limit-per-category",
        type=positive_int,
        default=None,
        help="Maximum product-family pages per category",
    )

    # --resume tells CrawlStore to retain its JSONL working data and checkpoint.
    # Without it, a run starts clean by deleting previous runtime state.
    crawl.add_argument("--resume", action="store_true")

    # page-size controls Algolia pagination. request-delay, timeout, and retries
    # are passed to the shared HttpClient, so they apply to every network call.
    crawl.add_argument("--page-size", type=positive_int, default=50)
    crawl.add_argument("--request-delay", type=float, default=1.0)
    crawl.add_argument("--timeout", type=float, default=30.0)
    crawl.add_argument("--max-retries", type=int, default=3)
    crawl.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def positive_int(value: str) -> int:
    """Argparse converter that rejects zero and negative limits/page sizes."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def load_categories(path: Path) -> list[Category]:
    """Turn JSON category dictionaries into typed Category objects."""
    data = json.loads(path.read_text(encoding="utf-8"))

    # Category(**item) maps {"name": ..., "url": ...} to
    # Category(name=..., url=...). Invalid or missing keys fail immediately.
    return [Category(**item) for item in data["categories"]]


def main() -> None:
    """Build the application, run the crawl, and print a machine-readable summary."""
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ),
    )
    if args.command != "crawl":
        raise SystemExit(2)

    # One shared client means all category, Algolia, and product-page requests
    # use the same rate limiter and retry policy.
    client = HttpClient(
        timeout_seconds=args.timeout,
        min_interval_seconds=max(args.request_delay, 0),
        max_retries=max(args.max_retries, 0),
    )

    # CrawlStore owns resumability, append-only working state, failures, and
    # final JSON/CSV exports. The orchestrator only tells it what happened.
    store = CrawlStore(args.output_dir, resume=args.resume)

    # Dependency injection keeps agents testable: they receive the HTTP client
    # and store instead of constructing network/filesystem dependencies inside.
    agent = CatalogScrapingAgent(
        client=client, store=store, page_size=args.page_size
    )

    # Workflow handoff:
    # category seeds -> orchestrator -> navigator -> extractor -> validator
    # -> store/export. main() does not need to know the scraping details.
    summary = agent.run(
        load_categories(args.config),
        limit_per_category=args.limit_per_category,
    )

    # JSON output is convenient for people, scripts, CI jobs, and schedulers.
    print(
        json.dumps(
            {
                "categories_attempted": summary.categories_attempted,
                "product_pages_attempted": summary.product_pages_attempted,
                "product_rows_exported": summary.product_rows_exported,
                "failures": summary.failures,
                "json_path": str(summary.json_path),
                "csv_path": str(summary.csv_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
