from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

from catalog_agent.http import HttpClient
from catalog_agent.models import Category, CategorySearchConfig
from catalog_agent.parsing import parse_algolia_config

LOGGER = logging.getLogger(__name__)


class NavigatorAgent:
    """Discover category search configuration and yield product-family listings.

    Safco's category grid is populated by Algolia in the browser. The navigator
    therefore has two phases:

    1. Download the category HTML and read its live Algolia configuration.
    2. Page through Algolia and yield one record per unique family/detail URL.

    It does not extract full product details or expand variations; that belongs
    to ExtractorAgent.
    """

    def __init__(self, client: HttpClient, page_size: int = 50) -> None:
        self.client = client
        self.page_size = page_size

    def discover(self, category: Category) -> CategorySearchConfig:
        """Read the live search credentials and category metadata from HTML."""
        LOGGER.info("Discovering category", extra={"url": category.url})
        document = self.client.get_text(category.url)

        # The page exposes window.algoliaConfig for its own frontend. Parsing it
        # at runtime avoids committing a short-lived search key and also gives
        # us the exact index, category path, and hierarchy level to query.
        return parse_algolia_config(document, category.url)

    def iter_product_families(
        self,
        config: CategorySearchConfig,
        *,
        limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Lazily yield unique family listings across all Algolia result pages.

        Because this function contains ``yield``, calling it does not fetch all
        pages immediately. Each iteration in the orchestrator resumes execution
        here, yielding records from the current page and requesting the next
        page only when needed.
        """
        # Algolia application IDs determine the public search host. Safco's base
        # index is extended with "_products" by CategorySearchConfig.
        endpoint = (
            f"https://{config.application_id.lower()}-dsn.algolia.net"
            f"/1/indexes/{config.product_index_name}/query"
        )

        # These are browser-exposed, search-only credentials discovered from
        # the category page, not administrative credentials or stored secrets.
        headers = {
            "x-algolia-application-id": config.application_id,
            "x-algolia-api-key": config.api_key,
        }

        # Example: category_level=1 produces "categories.level1", and the value
        # may be "Dental Supplies /// Dental Exam Gloves".
        facet = f"categories.level{config.category_level}"

        # Multiple Algolia records can point to the same family page. Deduping
        # URLs here prevents fetching and expanding the same page more than once
        # within this category traversal.
        seen_urls: set[str] = set()
        page = 0

        while True:
            # This is API pagination, equivalent to moving through frontend
            # result pages. page_size need not match the frontend's page size.
            params = urlencode(
                {
                    "query": "",
                    "page": page,
                    "hitsPerPage": self.page_size,
                    "facetFilters": json.dumps(
                        [f"{facet}:{config.category_path}"],
                        separators=(",", ":"),
                    ),
                }
            )
            result = self.client.post_json(
                endpoint, {"params": params}, headers
            )

            # "hits" contains the product-family search records for this page.
            # An empty page is a defensive stop even if nbPages is inconsistent.
            hits = result.get("hits", [])
            if not hits:
                break

            for hit in hits:
                # Grouped products normally expose family_url. Simple products
                # can use url directly, so both shapes enter the same pipeline.
                url = hit.get("family_url") or hit.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Preserve category context as extraction fallback. The product
                # page breadcrumb is preferred, but some pages may omit it.
                hit["_category_path"] = config.category_path
                yield hit

                # The optional POC limit is based on unique yielded families,
                # not API pages and not the number of final variation rows.
                if limit is not None and len(seen_urls) >= limit:
                    return

            page += 1

            # Algolia uses zero-based pages. If nbPages is 4, valid pages are
            # 0, 1, 2, and 3; after page 3, page becomes 4 and traversal stops.
            if page >= int(result.get("nbPages", 0)):
                break
