from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from catalog_agent.ai import AIEnrichmentAgent, OpenAIResponsesClient
from catalog_agent.alternatives import InferredAlternativeMatcher
from catalog_agent.http import HttpClient
from catalog_agent.models import Category
from catalog_agent.orchestrator import CatalogScrapingAgent, CrawlSummary
from catalog_agent.storage import CrawlStore


def load_env_file(path: Path = Path(".env")) -> None:
    """Load simple dotenv values without overriding the process environment."""
    if not path.is_file():
        return

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ValueError(
                f"{path}:{line_number}: expected KEY=value"
            )

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise ValueError(
                f"{path}:{line_number}: invalid environment variable name"
            )
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]

        os.environ.setdefault(key, value)


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

    state_mode = crawl.add_mutually_exclusive_group()
    state_mode.add_argument(
        "--resume",
        action="store_true",
        help="Retain existing checkpoint state (also happens automatically)",
    )
    state_mode.add_argument(
        "--fresh",
        action="store_true",
        help="Discard existing checkpoint state and start a new crawl",
    )

    # page-size controls Algolia pagination. request-delay, timeout, and retries
    # are passed to the shared HttpClient, so they apply to every network call.
    crawl.add_argument("--page-size", type=positive_int, default=50)
    crawl.add_argument("--request-delay", type=float, default=0.05)
    crawl.add_argument("--timeout", type=float, default=30.0)
    crawl.add_argument("--max-retries", type=int, default=3)
    crawl.add_argument(
        "--ai-enrichment",
        action="store_true",
        help="Use OpenAI for missing pack sizes and inferred alternatives",
    )
    crawl.add_argument(
        "--openai-model",
        default=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        help="OpenAI model used when --ai-enrichment is enabled",
    )
    crawl.add_argument("--max-alternatives", type=positive_int, default=3)
    crawl.add_argument(
        "--alternative-min-score",
        type=probability,
        default=0.45,
        help="Minimum inferred-alternative similarity score (0 to 1)",
    )
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


def probability(value: str) -> float:
    """Argparse converter for normalized scores and confidence values."""
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def load_categories(path: Path) -> list[Category]:
    """Turn JSON category dictionaries into typed Category objects."""
    data = json.loads(path.read_text(encoding="utf-8"))

    # Category(**item) maps {"name": ..., "url": ...} to
    # Category(name=..., url=...). Invalid or missing keys fail immediately.
    return [Category(**item) for item in data["categories"]]


def print_summary(summary: CrawlSummary) -> None:
    """Print the run result in a stable machine-readable form."""
    print(
        json.dumps(
            {
                "categories_attempted": summary.categories_attempted,
                "product_pages_attempted": summary.product_pages_attempted,
                "product_rows_exported": summary.product_rows_exported,
                "failures": summary.failures,
                "interrupted": summary.interrupted,
                "json_path": str(summary.json_path),
                "csv_path": str(summary.csv_path),
            },
            indent=2,
        )
    )


def main() -> None:
    """Build the application, run the crawl, and print a machine-readable summary."""
    # Load local development secrets before parser defaults read OPENAI_MODEL.
    # Real process variables take precedence over values in this file.
    load_env_file()
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

    enrichment_agent = None
    if args.ai_enrichment:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise SystemExit(
                "OPENAI_API_KEY must be set when --ai-enrichment is enabled"
            )
        enrichment_agent = AIEnrichmentAgent(
            OpenAIResponsesClient(
                api_key=api_key,
                model=args.openai_model,
                timeout_seconds=args.timeout,
                max_retries=max(args.max_retries, 0),
            )
        )

    # Matching is local and deterministic. Always enable it so a resumed crawl
    # can reuse tags already stored by an earlier AI-enriched run, even when
    # the current command does not make new OpenAI calls.
    alternative_matcher = InferredAlternativeMatcher(
        max_alternatives=args.max_alternatives,
        min_score=args.alternative_min_score,
    )

    # Existing durable state resumes automatically so rerunning after Ctrl+C
    # cannot accidentally erase progress. --fresh is the explicit reset path.
    resume = args.resume or (
        not args.fresh and CrawlStore.has_resumable_state(args.output_dir)
    )
    if resume and not args.resume:
        logging.getLogger(__name__).info(
            "Existing crawl state detected; resuming automatically"
        )
    store = CrawlStore(args.output_dir, resume=resume)

    # Dependency injection keeps agents testable: they receive the HTTP client
    # and store instead of constructing network/filesystem dependencies inside.
    agent = CatalogScrapingAgent(
        client=client,
        store=store,
        page_size=args.page_size,
        enrichment_agent=enrichment_agent,
        alternative_matcher=alternative_matcher,
    )

    # Workflow handoff:
    # category seeds -> orchestrator -> navigator -> extractor -> validator
    # -> store/export. main() does not need to know the scraping details.
    try:
        summary = agent.run(
            load_categories(args.config),
            limit_per_category=args.limit_per_category,
        )
    except KeyboardInterrupt:
        logging.getLogger(__name__).warning(
            "Crawl interrupted; exporting completed rows before exit"
        )
        summary = agent.export_current()
        summary.interrupted = True
        print_summary(summary)
        raise SystemExit(130)

    print_summary(summary)


if __name__ == "__main__":
    main()
