from __future__ import annotations

from typing import Any


class InferredAlternativeMatcher:
    """Rank catalog alternatives from normalized tags using deterministic math."""

    def __init__(
        self,
        *,
        max_alternatives: int = 3,
        min_score: float = 0.45,
    ) -> None:
        self.max_alternatives = max_alternatives
        self.min_score = min_score

    def apply(
        self, products: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for product in products:
            ranked: list[tuple[float, dict[str, Any], list[str]]] = []
            for candidate in products:
                if not self._eligible(product, candidate):
                    continue
                score, shared_tags = self._score(product, candidate)
                if score >= self.min_score:
                    ranked.append((score, candidate, shared_tags))

            ranked.sort(
                key=lambda item: (-item[0], str(item[1].get("sku", "")))
            )
            product["alternative_products"] = [
                self._alternative(candidate, score, shared_tags)
                for score, candidate, shared_tags in ranked[
                    : self.max_alternatives
                ]
            ]
        return products

    @staticmethod
    def _eligible(
        product: dict[str, Any], candidate: dict[str, Any]
    ) -> bool:
        if product.get("sku") == candidate.get("sku"):
            return False
        if product.get("product_url") == candidate.get("product_url"):
            return False
        return bool(product.get("normalized_tags")) and bool(
            candidate.get("normalized_tags")
        )

    @staticmethod
    def _score(
        product: dict[str, Any], candidate: dict[str, Any]
    ) -> tuple[float, list[str]]:
        product_tags = set(product.get("normalized_tags", []))
        candidate_tags = set(candidate.get("normalized_tags", []))
        shared_tags = sorted(product_tags & candidate_tags)
        union = product_tags | candidate_tags
        tag_similarity = len(shared_tags) / len(union) if union else 0

        product_category = InferredAlternativeMatcher._leaf_category(product)
        candidate_category = InferredAlternativeMatcher._leaf_category(
            candidate
        )
        same_leaf_category = bool(product_category) and (
            product_category == candidate_category
        )
        same_brand = bool(product.get("brand")) and (
            str(product.get("brand")).casefold()
            == str(candidate.get("brand")).casefold()
        )
        score = (
            0.65 * tag_similarity
            + 0.25 * float(same_leaf_category)
            + 0.10 * float(same_brand)
        )
        return round(score, 4), shared_tags

    @staticmethod
    def _leaf_category(product: dict[str, Any]) -> str:
        hierarchy = product.get("category_hierarchy") or []
        return str(hierarchy[-1]).casefold() if hierarchy else ""

    @staticmethod
    def _alternative(
        candidate: dict[str, Any],
        score: float,
        shared_tags: list[str],
    ) -> dict[str, Any]:
        reason = (
            "Shared normalized tags: " + ", ".join(shared_tags)
            if shared_tags
            else "Same catalog category"
        )
        return {
            "sku": candidate.get("sku"),
            "product_name": candidate.get("product_name"),
            "product_url": candidate.get("product_url"),
            "score": score,
            "reason": reason,
            "source": "inferred",
            "method": "llm_normalized_tags_v1",
        }
