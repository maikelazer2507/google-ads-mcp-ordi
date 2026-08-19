# Copyright 2026 Google LLC.

"""Read-only Google Ads test-account contract test.

This suite is intentionally excluded from normal unit tests by requiring an
explicit test customer environment variable. GitHub runs it only from a
protected, manually dispatched environment.
"""

import os
import unittest


@unittest.skipUnless(
    os.environ.get("GOOGLE_ADS_TEST_CUSTOMER_ID"),
    "Google Ads test-account credentials are not configured",
)
class GoogleAdsReadIntegrationTest(unittest.TestCase):
    def test_scoped_customer_query(self):
        customer_id = os.environ["GOOGLE_ADS_TEST_CUSTOMER_ID"].replace("-", "")
        os.environ["GOOGLE_ADS_MCP_READ_CUSTOMER_IDS"] = customer_id

        from ads_mcp.tools.search import search

        rows = search(
            customer_id,
            [
                "customer.id",
                "customer.descriptive_name",
                "customer.currency_code",
                "customer.time_zone",
                "customer.test_account",
            ],
            "customer",
            limit=1,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0]["customer.id"]), customer_id)
        self.assertTrue(rows[0]["customer.test_account"])


if __name__ == "__main__":
    unittest.main()
