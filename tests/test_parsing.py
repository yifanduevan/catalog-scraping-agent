from pathlib import Path
import unittest

from catalog_agent.parsing import (
    find_json_ld,
    parse_algolia_config,
    parse_initial_images,
    parse_json_ld,
    parse_master_data,
    parse_pack_size,
    parse_price_from_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


class ParsingTests(unittest.TestCase):
    def test_parses_live_category_configuration_shape(self) -> None:
        document = (FIXTURES / "category.html").read_text()
        config = parse_algolia_config(
            document, "https://example.test/catalog/gloves"
        )
        self.assertEqual(config.category_id, "385")
        self.assertEqual(config.category_level, 1)
        self.assertEqual(
            config.product_index_name, "store_default_products"
        )

    def test_parses_product_json_ld_and_grouped_variants(self) -> None:
        document = (FIXTURES / "product.html").read_text()
        product = find_json_ld(parse_json_ld(document), "Product")
        variants = parse_master_data(document)
        self.assertEqual(product["sku"], "DRCBT")
        self.assertEqual(variants["3021302"]["manufacturer_part_number"], "43932")
        self.assertEqual(
            parse_initial_images(document),
            ["https://example.test/aquasoft-full.jpg"],
        )

    def test_extracts_pack_sizes(self) -> None:
        self.assertEqual(parse_pack_size("X-small, 300/box"), "300/box")
        self.assertEqual(parse_pack_size("Box of 50"), "Box of 50")
        self.assertIsNone(parse_pack_size("Medium blue"))

    def test_extracts_nested_algolia_price(self) -> None:
        self.assertEqual(
            parse_price_from_html({"USD": {"default": 19.99}}),
            19.99,
        )


if __name__ == "__main__":
    unittest.main()
