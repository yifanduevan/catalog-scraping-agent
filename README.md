# Catalog Scraping Agent

A runnable proof of concept for data scraping. It
discovers products from dynamic category search, enriches them
from product detail pages, expands grouped products into SKU-level rows,
validates/deduplicates the result, checkpoints progress, and exports JSON/CSV.

## Quick start

Requirements: Python 3.11+ and outbound HTTPS access.

```bash
python3 -m catalog_agent crawl --limit-per-category 2
```

Outputs are written to `output/sample/products.json` and
`output/sample/products.csv`. A limited run is intentional for review; omit
`--limit-per-category` to traverse every unique family URL returned by the
configured categories.

Interrupted runs resume automatically when the same output directory contains
checkpoint state:

```bash
python3 -m catalog_agent crawl
```

Pressing `Ctrl+C` exports all completed rows before exiting. `--resume` remains
available as an explicit option. Use `--fresh` to discard existing state and
start over.

Enable the optional OpenAI enrichment path:

```bash
cp .env.example .env
# Edit .env and replace the placeholder OPENAI_API_KEY.
python3 -m catalog_agent crawl --ai-enrichment --limit-per-category 2
```

The CLI loads `.env` automatically from the current directory. The file is
ignored by Git, while `.env.example` documents the required variables without
containing a real secret. Variables already exported in the shell take
precedence over `.env`.

This uses deterministic pack-size rules first. The LLM is called only when a
pack size is still missing, and it also creates normalized tags used to infer
up to three alternatives from products collected in the same crawl.
Alternative matching itself is local and runs on every export, so resumed
crawls can reuse tags already saved by an earlier AI-enriched run.
An LLM-derived pack size is accepted only when its supporting evidence is
supported by the scraped source text. Formatting differences such as
`100/bag` versus `100 per bag`, packaging aliases such as `bx`/`box`, and
`cc`/`ml` are normalized. A matching number alone is not sufficient, and
capacity or operating specifications such as "holds 3 boxes" and "priming
volume" are rejected. Rejected results are logged with the proposed value and
evidence while the product is still saved.

Run tests:

```bash
python3 -m unittest discover -v
```

Optional editable installation exposes the `catalog-agent` command:

```bash
python3 -m pip install -e . --no-build-isolation
catalog-agent crawl --limit-per-category 2
```

## Target categories

The live category linked by Safco's navigation is:
`https://www.safcodental.com/catalog/<CATEGORY_NAME>`

The crawl can be scaled to other categories by changing
`config/categories.json`.

For now, both categories are configured in `config/categories.json`; no Algolia key,
index, category ID, or category path is hard-coded. Those values are discovered
from each category page because Safco generates a short-lived browser search
key.

## Why Algolia instead of Playwright

Safco's frontend loads category results from a browser-exposed Algolia search
API. Querying that same structured source is faster, easier to paginate and
retry, and less sensitive to visual layout or CSS selector changes than driving
the page with Playwright. For this defined Safco scope, it provides the more
stable production path. Playwright remains a useful fallback if future pages
require JavaScript execution or stop exposing equivalent structured data.

## Architecture

```text
config
  |
  v
NavigatorAgent ----> category HTML ----> live Algolia configuration
  |                                      |
  +---------------- public search API <--+
  |
  v
unique product-family URLs
  |
  v
ExtractorAgent ----> Product JSON-LD + grouped-product masterData
  |
  v
SKU-level Product rows
  |
  v
ValidatorAgent ----> required-field checks + SKU deduplication
  |
  v
AIEnrichmentAgent -> missing pack-size fallback + normalized product tags
  |
  v
AlternativeMatcher -> deterministic catalog similarity ranking
  |
  v
CrawlStore --------> checkpoint + JSONL state + JSON/CSV exports
```

### Agent responsibilities

- **Navigator agent:** classifies a live category page, discovers its transient
  Algolia settings, paginates the dynamic index, and deduplicates family URLs.
- **Extractor agent:** reads standards-based Product/Breadcrumb JSON-LD and
  Safco's grouped-product data, then emits one normalized row per purchasable
  SKU variation.
- **Validator agent:** normalizes required text fields, rejects incomplete
  records, and deduplicates SKUs.
- **AI enrichment agent (optional):** uses schema-constrained output to fill
  missing pack sizes only when the model returns supporting source text, and
  produces normalized product-type/attribute tags.
- **Alternative matcher (optional):** ranks different product families using
  tag overlap, leaf category, and brand. It stores a score and explanation and
  labels every result as inferred rather than site-provided.
- **Orchestrator/recovery:** isolates failures per category/product, retries
  transient HTTP errors, persists completed URLs, and supports `--resume`.

This is deliberately "agent-based" through narrow, testable responsibilities,
not through an LLM call for every extraction field. Safco's regular structure
is more reliable and cheaper to parse deterministically; the LLM is an opt-in
fallback for ambiguous pack-size prose and semantic product normalization.

## Output schema

Each JSON record and CSV row represents a SKU, not merely a product family.

| Field | Type | Notes |
|---|---|---|
| `source` | string | Source domain |
| `source_product_id` | string/null | Safco/Magento child product ID |
| `parent_sku` | string/null | Grouped product SKU when applicable |
| `sku` | string | Deduplication/idempotency key |
| `product_name` | string | Variant-specific name when available |
| `brand` | string/null | Manufacturer/brand |
| `manufacturer_part_number` | string/null | Manufacturer code |
| `category_hierarchy` | array | Breadcrumb categories |
| `product_url` | string | Canonical family/detail URL |
| `price` | number/null | Publicly visible unit price |
| `currency` | string/null | Normally `USD` |
| `unit_pack_size` | string/null | Parsed pack expression |
| `unit_pack_size_source` | string/null | `rule` or evidence-backed `llm` |
| `availability` | string/null | Public stock label |
| `description` | string/null | Plain-text family description |
| `normalized_tags` | array | LLM-normalized type and attributes |
| `specifications` | object | Labeled specs plus variant description |
| `image_urls` | array | Parent and variant image URLs |
| `alternative_products` | array | Scored, explicitly inferred recommendations |
| `scraped_at` | ISO-8601 string | UTC extraction timestamp |

Nested arrays/objects are JSON-encoded in CSV.

## Failure handling and controls

- Global request pacing defaults to one request/second and is configurable with
  `--request-delay`.
- Transient network errors and HTTP 408/429/5xx responses use exponential
  backoff with jitter.
- Product failures are recorded in `failures.jsonl` without stopping the crawl.
- AI enrichment failures are also recorded without rejecting the otherwise
  valid product.
- `state/checkpoint.json` records completed product URLs atomically.
- `state/products.jsonl` is append-only working state; final exports deduplicate
  by SKU, making resume/export idempotent.
- Existing state is resumed automatically, and `Ctrl+C` triggers a partial
  export before the process exits with status 130.
- Category URLs, page size, limits, timeouts, retries, output path, and logging
  level are configuration/CLI driven.
- No secret is required for the default deterministic crawl. AI enrichment
  reads `OPENAI_API_KEY` from the environment and never writes it to output.
- The Algolia search-only key is already exposed to every site visitor and is
  fetched fresh rather than committed.

## Production path insights

1. Move crawl jobs and per-URL state to a durable queue/database (for example
   SQS plus Postgres) and use a uniqueness constraint on `(source, sku)`.
2. Split navigation and extraction into independently scalable workers with
   bounded concurrency per domain and centralized robots/terms policy.
3. Add a Playwright fallback worker for pages that cannot be recovered from
   HTML/embedded structured data (Algolia).
4. Package as a container and schedule with ECS/Kubernetes/Cloud Run Jobs,
   injecting runtime configuration through environment variables or a secret
   manager.

## Monitoring and data quality

Track crawl and data metrics by category and parser version:

- discovered family URLs, attempted pages, successful pages, retries, 429s,
  latency, and terminal failures;
- products per family, missing-field rates, duplicate-SKU rates, and price/
  availability distributions;
- extraction coverage for brand, manufacturer code, pack size, description,
  specifications, and images;
- sudden count changes against the prior successful run;
- HTML/schema fingerprints and JSON-LD/masterData parse failures.

## Current limitations

- Alternatives are catalog-level inferences, not substitutes asserted by
  Safco. Their quality depends on LLM tags and on how much of the catalog was
  collected; small limited runs provide fewer candidates.
- The LLM fallback adds API cost and latency and requires `OPENAI_API_KEY`.
- Evidence validation reduces pack-size hallucination risk but does not replace
  downstream data-quality monitoring.
- The prototype is single-process and can be optimized by parallelization.
- Category discovery is configured from the two supplied seed URLs. Full-site
  category-tree discovery would start from Safco's navigation or sitemap and
  feed the same classification/extraction pipeline.
- Site terms and robots policy should be reviewed with the site owner before a
  production-scale crawl.
