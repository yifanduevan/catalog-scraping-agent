import unittest

from catalog_agent.alternatives import InferredAlternativeMatcher


class AlternativeMatcherTests(unittest.TestCase):
    def test_ranks_similar_products_and_excludes_same_family(self):
        products = [
            {
                "sku": "A",
                "product_name": "Blue nitrile gloves",
                "product_url": "https://example.test/a",
                "brand": "Brand A",
                "category_hierarchy": ["Supplies", "Nitrile gloves"],
                "normalized_tags": [
                    "exam glove",
                    "nitrile",
                    "powder-free",
                ],
            },
            {
                "sku": "A-SMALL",
                "product_name": "Blue nitrile gloves small",
                "product_url": "https://example.test/a",
                "brand": "Brand A",
                "category_hierarchy": ["Supplies", "Nitrile gloves"],
                "normalized_tags": [
                    "exam glove",
                    "nitrile",
                    "powder-free",
                ],
            },
            {
                "sku": "B",
                "product_name": "Black nitrile gloves",
                "product_url": "https://example.test/b",
                "brand": "Brand B",
                "category_hierarchy": ["Supplies", "Nitrile gloves"],
                "normalized_tags": [
                    "exam glove",
                    "nitrile",
                    "powder-free",
                ],
            },
            {
                "sku": "C",
                "product_name": "Cotton rolls",
                "product_url": "https://example.test/c",
                "brand": "Brand C",
                "category_hierarchy": ["Supplies", "Cotton products"],
                "normalized_tags": ["cotton roll", "absorbent"],
            },
        ]

        result = InferredAlternativeMatcher().apply(products)

        alternatives = result[0]["alternative_products"]
        self.assertEqual([item["sku"] for item in alternatives], ["B"])
        self.assertEqual(alternatives[0]["source"], "inferred")
        self.assertEqual(
            alternatives[0]["method"], "llm_normalized_tags_v1"
        )

    def test_missing_categories_do_not_receive_category_score(self):
        products = [
            {
                "sku": "A",
                "product_url": "https://example.test/a",
                "normalized_tags": ["glove", "nitrile"],
            },
            {
                "sku": "B",
                "product_url": "https://example.test/b",
                "normalized_tags": ["glove", "latex"],
            },
        ]

        result = InferredAlternativeMatcher(min_score=0.3).apply(products)

        self.assertEqual(result[0]["alternative_products"], [])


if __name__ == "__main__":
    unittest.main()
