"""Run: python3 -m unittest -v  (from tools/)"""
import unittest
from pathlib import Path

from check_webhook_config import check, load

ROOT = Path(__file__).resolve().parent.parent


class WebhookConfigTests(unittest.TestCase):
    def test_fixed_config_passes(self):
        self.assertEqual(check(load(ROOT / "deploy/fixed")), [])

    def test_outage_config_fails_every_rule(self):
        errs = "\n".join(check(load(ROOT / "deploy/broken")))
        for rule in ("W1", "W2", "W3", "W4"):
            self.assertIn(rule, errs)

    def test_custom_exempt_label_is_not_enough(self):
        docs = load(ROOT / "deploy/fixed")
        wh = next(d for d in docs if d["kind"] == "ValidatingWebhookConfiguration")["webhooks"][0]
        wh["namespaceSelector"] = {"matchExpressions": [
            {"key": "admission-guard.io/exempt", "operator": "DoesNotExist"}]}
        self.assertTrue(any("W1" in e for e in check(docs)))

    def test_fail_open_webhook_is_not_flagged_for_deadlock(self):
        docs = load(ROOT / "deploy/broken")
        next(d for d in docs if d["kind"] == "ValidatingWebhookConfiguration")["webhooks"][0]["failurePolicy"] = "Ignore"
        self.assertEqual(check(docs), [])


if __name__ == "__main__":
    unittest.main()
