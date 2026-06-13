import unittest

from catalog_agent.ai import AIEnrichmentAgent
from catalog_agent.models import Product


class FakeStructuredClient:
    def __init__(self, responses):
        self.responses = iter(responses)

    def generate(self, **kwargs):
        return next(self.responses)


def product(**overrides):
    values = {
        "source": "example.test",
        "source_product_id": "1",
        "parent_sku": None,
        "sku": "SKU-1",
        "product_name": "Procedure kit",
        "brand": "Example",
        "manufacturer_part_number": None,
        "category_hierarchy": ["Supplies", "Procedure kits"],
        "product_url": "https://example.test/product",
        "price": 10.0,
        "currency": "USD",
        "unit_pack_size": None,
        "availability": "In stock",
        "description": "Ten sleeves with six sterile items each.",
    }
    values.update(overrides)
    return Product(**values)


class AIEnrichmentTests(unittest.TestCase):
    def test_uses_evidence_backed_pack_size_and_normalized_tags(self):
        client = FakeStructuredClient(
            [
                {
                    "pack_size": "60 items/case",
                    "evidence": "Ten sleeves with six sterile items each.",
                    "confidence": 0.92,
                },
                {
                    "product_type": "procedure kit",
                    "attributes": ["Sterile", "single use", "sterile"],
                },
            ]
        )
        item = product()

        result = AIEnrichmentAgent(client).enrich([item])

        self.assertEqual(result.errors, [])
        self.assertEqual(item.unit_pack_size, "60 items/case")
        self.assertEqual(item.unit_pack_size_source, "llm")
        self.assertEqual(
            item.normalized_tags,
            ["procedure kit", "sterile", "single use"],
        )

    def test_rejects_pack_size_when_evidence_is_not_in_source(self):
        client = FakeStructuredClient(
            [
                {
                    "pack_size": "60 items/case",
                    "evidence": "Sixty items per case",
                    "confidence": 0.95,
                },
                {"product_type": "procedure kit", "attributes": []},
            ]
        )
        item = product()

        result = AIEnrichmentAgent(client).enrich([item])

        self.assertIsNone(item.unit_pack_size)
        self.assertEqual(len(result.errors), 1)

    def test_accepts_equivalent_pack_size_formatting(self):
        client = FakeStructuredClient(
            [
                {
                    "pack_size": "100 per bag",
                    "evidence": "Medium - 100 / bag",
                    "confidence": 0.95,
                },
                {"product_type": "exam glove", "attributes": ["nitrile"]},
            ]
        )
        item = product(
            product_name="Compac nitrile gloves medium 100/bag",
            description="Powder-free exam gloves.",
            specifications={"variant": "Medium, 100/bag"},
        )

        result = AIEnrichmentAgent(client).enrich([item])

        self.assertEqual(result.errors, [])
        self.assertEqual(item.unit_pack_size, "100 per bag")

    def test_rejects_number_only_evidence(self):
        client = FakeStructuredClient(
            [
                {
                    "pack_size": "100/bag",
                    "evidence": "100",
                    "confidence": 0.99,
                },
                {"product_type": "exam glove", "attributes": []},
            ]
        )
        item = product(
            product_name="Model 100 exam glove",
            description="Medium blue glove.",
        )

        result = AIEnrichmentAgent(client).enrich([item])

        self.assertIsNone(item.unit_pack_size)
        self.assertEqual(len(result.errors), 1)

    def test_accepts_packaging_aliases(self):
        cases = [
            ("3 pairs/package", "3 pairs/pkg"),
            ("12/box", "12/bx"),
            ("50/case", "50/cs"),
            ("0.5 ml", "0.5cc jar"),
            ("16 bags/sleeve", "100ml bag, sleeve of 16"),
        ]
        for pack_size, source in cases:
            with self.subTest(pack_size=pack_size, source=source):
                self.assertTrue(
                    AIEnrichmentAgent._pack_evidence_supported(
                        pack_size=pack_size,
                        evidence=source,
                        source_text=source,
                    )
                )

    def test_accepts_exact_descriptive_package(self):
        self.assertTrue(
            AIEnrichmentAgent._pack_evidence_supported(
                pack_size="standard package",
                evidence="standard package",
                source_text="Standard package, regular body and regular set",
            )
        )

    def test_rejects_capacity_and_operating_measurements(self):
        cases = [
            ("3 boxes", "Triple (holds 3 boxes)"),
            ("17 ml", "17ml priming volume"),
            ("15 ml", "15 drops/ml"),
        ]
        for pack_size, source in cases:
            with self.subTest(pack_size=pack_size, source=source):
                self.assertFalse(
                    AIEnrichmentAgent._pack_evidence_supported(
                        pack_size=pack_size,
                        evidence=source,
                        source_text=source,
                    )
                )


if __name__ == "__main__":
    unittest.main()
