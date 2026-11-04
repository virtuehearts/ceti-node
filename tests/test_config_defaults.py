"""Default-model resolution tests: the System tab must show the model
a startup actually loads."""
import unittest

from hub.config import default_model


class DefaultModelTest(unittest.TestCase):
    def test_chat_default_is_ministral(self):
        m = default_model("chat")
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], "ministral-uncensored")
        self.assertTrue(m["default"])
        self.assertIn("Ministral", m["name"])

    def test_embed_default_is_embeddinggemma(self):
        m = default_model("embed")
        self.assertIsNotNone(m)
        self.assertEqual(m["id"], "embeddinggemma")
        self.assertTrue(m["default"])

    def test_unknown_role_is_none(self):
        self.assertIsNone(default_model("nope"))


if __name__ == "__main__":
    unittest.main()
