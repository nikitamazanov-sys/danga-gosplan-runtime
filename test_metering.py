import json
import unittest

from worker import usage_metrics


class UsageMetricsTests(unittest.TestCase):
    def test_known_model(self):
        sample = json.dumps({"usage": {"input_tokens": 12500, "output_tokens": 8500,
                       "input_tokens_details": {"cached_tokens": 2500}}}).encode()
        result = usage_metrics(sample, "gpt-6.1-terra")
        self.assertEqual((result["input"], result["cached"], result["output"]), (12500, 2500, 8500))
        self.assertAlmostEqual(result["estimated_usd"], 0.052625, places=6)

    def test_unknown_model_does_not_invent_cost(self):
        sample = b'{"usage":{"input_tokens":10,"output_tokens":20}}'
        self.assertIsNone(usage_metrics(sample, "future-model")["estimated_usd"])

    def test_missing_or_invalid_usage(self):
        self.assertIsNone(usage_metrics(b'{"output":[]}', "gpt-6.1-terra"))
        self.assertIsNone(usage_metrics(b'broken', "gpt-6.1-terra"))
        self.assertIsNone(usage_metrics(b'{"usage":{"input_tokens":3,"output_tokens":1,"input_tokens_details":{"cached_tokens":9}}}', "gpt-6.1-terra"))


if __name__ == "__main__":
    unittest.main()
