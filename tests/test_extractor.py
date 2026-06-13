from pathlib import Path
import unittest

from catalog_agent.extractor import ExtractorAgent

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    def get_text(self, url: str) -> str:
        return (FIXTURES / "product.html").read_text()


class ExtractorTests(unittest.TestCase):
    def test_expands_grouped_product_to_variant_rows(self) -> None:
        agent = ExtractorAgent(FakeClient())
        products = agent.extract(
            "https://example.test/product/aquasoft",
            {
                "objectID": "95896",
                "manufacturer_name": "Halyard",
                "_category_path": (
                    "Dental Supplies /// Dental Exam Gloves"
                ),
            },
        )
        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product.parent_sku, "DRCBT")
        self.assertEqual(product.sku, "3021302")
        self.assertEqual(product.unit_pack_size, "300/box")
        self.assertEqual(
            product.category_hierarchy,
            ["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"],
        )
        self.assertEqual(product.specifications["Thickness"], "3.1 mils")


if __name__ == "__main__":
    unittest.main()

