import json
import tempfile
import unittest
from pathlib import Path

from catalog_agent.storage import CrawlStore


class StorageTests(unittest.TestCase):
    def test_resume_ignores_incomplete_final_jsonl_row(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = CrawlStore(output_dir, resume=True)
            store.raw_path.write_text(
                json.dumps({"sku": "complete"}) + '\n{"sku": "partial',
                encoding="utf-8",
            )

            products = store.load_products()

            self.assertEqual(products, [{"sku": "complete"}])


if __name__ == "__main__":
    unittest.main()
