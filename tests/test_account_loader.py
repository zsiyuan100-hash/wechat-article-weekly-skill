import unittest
from pathlib import Path

from scripts.account_loader import load_accounts


class AccountLoaderTest(unittest.TestCase):
    def test_loads_shared_xlsx(self):
        root = Path(__file__).resolve().parents[1]
        accounts = load_accounts(root / "accounts.xlsx")
        self.assertGreater(len(accounts), 50)
        self.assertEqual(accounts[0]["name"], "PaperAgent")
        self.assertTrue(all(not item["name"].startswith("http") for item in accounts))
        self.assertTrue(any(item["category"] for item in accounts))


if __name__ == "__main__":
    unittest.main()
