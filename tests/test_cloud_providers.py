"""Cloud provider map tests: OrcaRouter sits above OpenRouter in the
provider list and resolves through the same OpenAI-compatible path."""
import os
import unittest

from hub.cloud import PROVIDERS, key_for


class CloudProvidersTest(unittest.TestCase):
    def test_orcarouter_entry(self):
        entry = PROVIDERS["orcarouter"]
        self.assertEqual(entry["base"], "https://api.orcarouter.ai/v1")
        self.assertEqual(entry["env"], "ORCAROUTER_API_KEY")
        self.assertIn("orcarouter/auto", entry["suggest"])

    def test_key_for_prefers_supplied_then_env(self):
        self.assertEqual(key_for("orcarouter", "sk-orca-test"), "sk-orca-test")
        old = os.environ.get("ORCAROUTER_API_KEY")
        os.environ["ORCAROUTER_API_KEY"] = "sk-orca-env"
        try:
            self.assertEqual(key_for("orcarouter"), "sk-orca-env")
        finally:
            if old is None:
                os.environ.pop("ORCAROUTER_API_KEY", None)
            else:
                os.environ["ORCAROUTER_API_KEY"] = old

    def test_all_providers_have_base_env_suggest(self):
        for name, entry in PROVIDERS.items():
            self.assertTrue(entry["base"].startswith("https://"),
                            name)
            self.assertTrue(entry["env"].endswith("_API_KEY"), name)
            self.assertTrue(entry["suggest"], name)


if __name__ == "__main__":
    unittest.main()
