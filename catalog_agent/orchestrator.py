from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from catalog_agent.ai import AIEnrichmentAgent
from catalog_agent.alternatives import InferredAlternativeMatcher
from catalog_agent.extractor import ExtractorAgent
from catalog_agent.http import HttpClient
from catalog_agent.models import Category
from catalog_agent.navigator import NavigatorAgent
from catalog_agent.storage import CrawlStore
from catalog_agent.validator import ValidatorAgent

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CrawlSummary:
    """Small run-level report returned to the CLI."""

    categories_attempted: int = 0
    product_pages_attempted: int = 0
    product_rows_exported: int = 0
    failures: int = 0
    interrupted: bool = False
    json_path: Path | None = None
    csv_path: Path | None = None


class CatalogScrapingAgent:
    """Coordinate agents and isolate failures at useful recovery boundaries.

    The orchestrator deliberately contains little parsing logic. Its job is to
    move data through the workflow:

    category seed -> discovered search config -> family listing -> SKU rows
    -> validation -> checkpointed storage -> final exports.
    """

    def __init__(
        self,
        *,
        client: HttpClient,
        store: CrawlStore,
        page_size: int = 50,
        enrichment_agent: AIEnrichmentAgent | None = None,
        alternative_matcher: InferredAlternativeMatcher | None = None,
    ) -> None:
        # Navigator and extractor share one HttpClient, so requests from both
        # agents obey the same global pacing and retry policy.
        self.navigator = NavigatorAgent(client, page_size=page_size)
        self.extractor = ExtractorAgent(client)
        self.validator = ValidatorAgent()
        self.store = store
        self.enrichment_agent = enrichment_agent
        self.alternative_matcher = alternative_matcher

    def run(
        self,
        categories: list[Category],
        *,
        limit_per_category: int | None,
    ) -> CrawlSummary:
        """Crawl each category while allowing independent failures to continue."""
        summary = CrawlSummary()

        # A category is only a configured name and seed URL at this point.
        for category in categories:
            summary.categories_attempted += 1
            try:
                # Phase 1: download the category HTML and discover the live
                # Algolia key, application/index names, and category filter.
                config = self.navigator.discover(category)
            except Exception as exc:
                # If discovery fails, there is no safe way to traverse this
                # category, but the next configured category can still run.
                LOGGER.exception("Category discovery failed")
                self.store.record_failure(
                    url=category.url, stage="category_discovery", error=str(exc)
                )
                summary.failures += 1
                continue

            try:
                # Phase 2: this returns a lazy generator. Algolia pages are
                # fetched incrementally as the for-loop requests more listings.
                families = self.navigator.iter_product_families(
                    config, limit=limit_per_category
                )
                for listing in families:
                    # A family is one product/detail page. It may later become
                    # one standalone SKU or many variation SKUs.
                    product_url = listing.get("family_url") or listing.get("url")

                    # Resume support skips pages successfully checkpointed by an
                    # earlier run. Missing URLs cannot be extracted.
                    if not product_url or self.store.is_completed(product_url):
                        continue
                    summary.product_pages_attempted += 1
                    try:
                        # Phase 3: enrich the lightweight Algolia listing with
                        # product-page JSON-LD and grouped-product masterData.
                        products = self.extractor.extract(product_url, listing)

                        # Phase 4: normalize required values, reject incomplete
                        # records, and deduplicate child SKUs within the family.
                        result = self.validator.validate(products)
                        if self.enrichment_agent and result.products:
                            enrichment = self.enrichment_agent.enrich(
                                result.products
                            )
                            result.products = enrichment.products
                            for error in enrichment.errors:
                                self.store.record_failure(
                                    url=product_url,
                                    stage="ai_enrichment",
                                    error=error,
                                )
                                summary.failures += 1
                        if result.products:
                            # Save accepted records before checkpointing the URL.
                            # This ordering avoids marking unsaved work complete.
                            self.store.save_products(result.products)

                        # Validation failures are data-quality failures. Valid
                        # siblings from the same family are still retained.
                        for error in result.errors:
                            self.store.record_failure(
                                url=product_url,
                                stage="validation",
                                error=error,
                            )
                            summary.failures += 1

                        # A family page is complete after its accepted products
                        # and validation failures have both been persisted.
                        self.store.mark_completed(product_url)
                    except Exception as exc:
                        # A broken product page should not terminate its category
                        # or the entire crawl. It remains uncheckpointed so a
                        # future --resume run can retry it.
                        LOGGER.exception(
                            "Product extraction failed",
                            extra={"url": product_url},
                        )
                        self.store.record_failure(
                            url=product_url,
                            stage="product_extraction",
                            error=str(exc),
                        )
                        summary.failures += 1
            except Exception as exc:
                # This boundary catches failures while requesting/iterating
                # Algolia pages. Already saved families remain checkpointed.
                LOGGER.exception("Category traversal failed")
                self.store.record_failure(
                    url=category.url,
                    stage="category_traversal",
                    error=str(exc),
                )
                summary.failures += 1

        return self.export_current(summary)

    def export_current(
        self, summary: CrawlSummary | None = None
    ) -> CrawlSummary:
        """Export all durable rows, including after an interrupted crawl."""
        summary = summary or CrawlSummary()
        final_products = self.store.load_products()
        if self.alternative_matcher:
            final_products = self.alternative_matcher.apply(final_products)
        json_path, csv_path, count = self.store.export(final_products)
        summary.json_path = json_path
        summary.csv_path = csv_path
        summary.product_rows_exported = count
        return summary
