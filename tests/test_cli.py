import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from catalog_agent.alternatives import InferredAlternativeMatcher
from catalog_agent.cli import load_env_file, main
from catalog_agent.orchestrator import CatalogScrapingAgent, CrawlSummary


class CliTests(unittest.TestCase):
    @staticmethod
    def _existing_state(output_dir: Path) -> Path:
        state_dir = output_dir / "state"
        state_dir.mkdir()
        marker = state_dir / "products.jsonl"
        marker.write_text('{"sku": "existing"}\n', encoding="utf-8")
        return marker

    def test_load_env_file_reads_values_without_overriding_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "# Local settings",
                        "OPENAI_API_KEY='from-file'",
                        "export OPENAI_MODEL=gpt-test",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "from-process"},
                clear=True,
            ):
                load_env_file(env_file)

                self.assertEqual(
                    os.environ["OPENAI_API_KEY"], "from-process"
                )
                self.assertEqual(os.environ["OPENAI_MODEL"], "gpt-test")

    def test_missing_ai_key_does_not_clear_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            marker = self._existing_state(output_dir)
            argv = [
                "catalog-agent",
                "crawl",
                "--ai-enrichment",
                "--output-dir",
                str(output_dir),
            ]

            with (
                patch.object(sys, "argv", argv),
                patch.dict(
                    os.environ, {"OPENAI_API_KEY": ""}, clear=True
                ),
                self.assertRaisesRegex(SystemExit, "OPENAI_API_KEY"),
            ):
                main()

            self.assertTrue(marker.exists())

    def test_existing_state_resumes_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            marker = self._existing_state(output_dir)
            argv = [
                "catalog-agent",
                "crawl",
                "--output-dir",
                str(output_dir),
            ]
            summary = CrawlSummary(
                json_path=output_dir / "products.json",
                csv_path=output_dir / "products.csv",
            )

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    CatalogScrapingAgent, "run", return_value=summary
                ),
                redirect_stdout(StringIO()),
            ):
                main()

            self.assertTrue(marker.exists())

    def test_resume_without_ai_still_enables_alternative_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._existing_state(output_dir)
            argv = [
                "catalog-agent",
                "crawl",
                "--output-dir",
                str(output_dir),
            ]
            summary = CrawlSummary(
                json_path=output_dir / "products.json",
                csv_path=output_dir / "products.csv",
            )

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    CatalogScrapingAgent, "run", return_value=summary
                ),
                patch.object(
                    CatalogScrapingAgent,
                    "__init__",
                    autospec=True,
                    return_value=None,
                ) as init,
                redirect_stdout(StringIO()),
            ):
                main()

            matcher = init.call_args.kwargs["alternative_matcher"]
            self.assertIsInstance(matcher, InferredAlternativeMatcher)
            self.assertIsNone(init.call_args.kwargs["enrichment_agent"])

    def test_fresh_discards_existing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            marker = self._existing_state(output_dir)
            argv = [
                "catalog-agent",
                "crawl",
                "--fresh",
                "--output-dir",
                str(output_dir),
            ]
            summary = CrawlSummary(
                json_path=output_dir / "products.json",
                csv_path=output_dir / "products.csv",
            )

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    CatalogScrapingAgent, "run", return_value=summary
                ),
                redirect_stdout(StringIO()),
            ):
                main()

            self.assertFalse(marker.exists())

    def test_keyboard_interrupt_exports_and_exits_with_130(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            self._existing_state(output_dir)
            argv = [
                "catalog-agent",
                "crawl",
                "--output-dir",
                str(output_dir),
            ]
            partial = CrawlSummary(
                product_rows_exported=3,
                json_path=output_dir / "products.json",
                csv_path=output_dir / "products.csv",
            )

            with (
                patch.object(sys, "argv", argv),
                patch.object(
                    CatalogScrapingAgent,
                    "run",
                    side_effect=KeyboardInterrupt,
                ),
                patch.object(
                    CatalogScrapingAgent,
                    "export_current",
                    return_value=partial,
                ) as export_current,
                redirect_stdout(StringIO()) as stdout,
                self.assertRaisesRegex(SystemExit, "130"),
            ):
                main()

            export_current.assert_called_once_with()
            self.assertTrue(partial.interrupted)
            self.assertIn('"interrupted": true', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
