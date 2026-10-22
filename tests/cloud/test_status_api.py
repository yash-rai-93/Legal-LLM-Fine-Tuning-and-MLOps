"""Read-only API tests using synthetic metadata; execute only in CodeBuild."""
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import unittest

from src.cloud.status_api import handle_request

RUN_ID = "1605eeda-3334-4fc4-a8f3-c5c047f75994"
API_ID = "fixtureapi"


class FakeTable:
    def __init__(self):
        self.calls = []
        self.item = {
            "registry_version": "contractnli-run-v1", "run_id": RUN_ID,
            "status": "COMPLETED", "operation": "data_preparation",
            "live_inference": False, "build_id": "legal-llm-cpu-check:" + RUN_ID,
            "source_commit": "a" * 40, "registry_commit": "b" * 40,
            "bucket": "fixture-bucket", "prefix": "runs/" + RUN_ID + "/",
            "registered_at": "2026-10-21T23:57:13+00:00",
            "manifest_created_at": "2026-10-21T23:57:00+00:00",
            "dataset_source": "https://stanfordnlp.github.io/contract-nli/",
            "dataset_license": "CC BY 4.0",
            "counts": {
                "train": {"documents": Decimal(423), "input_rows": Decimal(7191)},
                "validation": {"documents": Decimal(61), "input_rows": Decimal(1037)},
            },
            "artifacts": {},
            "contract": "SYNTHETIC PRIVATE BODY",
        }
        for name in ("manifest.json", "train.jsonl", "validation.jsonl"):
            self.item["artifacts"][name] = {
                "key": "runs/" + RUN_ID + "/" + name, "version_id": "fixture-version",
                "sha256": "c" * 64, "bytes": Decimal(1234), "encryption": "AES256",
                "contract": "SYNTHETIC PRIVATE BODY",
            }

    def get_item(self, *, Key, ConsistentRead):
        self.calls.append((Key, ConsistentRead))
        return {} if self.item is None else {"Item": deepcopy(self.item)}


class StatusApiTests(unittest.TestCase):
    def setUp(self):
        self.table, self.factory_calls = FakeTable(), 0
        self.event = {
            "version": "2.0", "routeKey": "GET /runs/{run_id}",
            "pathParameters": {"run_id": RUN_ID},
            "requestContext": {
                "apiId": API_ID, "http": {"method": "GET"},
                "authorizer": {"iam": {
                    "userArn": "arn:aws:iam::123456789012:user/fixture",
                    "accessKey": "SYNTHETIC CREDENTIAL MARKER",
                }},
            },
        }

    def handle(self):
        def factory():
            self.factory_calls += 1
            return self.table
        return handle_request(self.event, expected_api_id=API_ID, table_factory=factory)

    def test_success_returns_metadata_numbers_and_exact_object_versions(self):
        response = self.handle()
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["counts"]["train"], {"documents": 423, "input_rows": 7191})
        self.assertEqual(body["artifacts"]["train.jsonl"]["version_id"], "fixture-version")
        self.assertEqual(body["artifacts"]["train.jsonl"]["bytes"], 1234)
        self.assertEqual(self.table.calls, [({"run_id": RUN_ID}, True)])
        self.assertEqual(response["headers"]["cache-control"], "no-store")

    def test_response_excludes_contracts_credentials_and_unknown_fields(self):
        self.table.item["counts"]["train"]["source_clause"] = "SYNTHETIC PRIVATE BODY"
        body = self.handle()["body"]
        self.assertNotIn("SYNTHETIC PRIVATE BODY", body)
        self.assertNotIn("SYNTHETIC CREDENTIAL MARKER", body)
        self.assertNotIn("userArn", body)

    def test_missing_iam_context_is_forbidden_before_any_table_access(self):
        for authorizer in (None, {}, {"iam": {}}, {"jwt": {"userArn": "spoofed"}}):
            self.event["requestContext"]["authorizer"] = authorizer
            self.assertEqual(self.handle()["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.table.calls, [])

    def test_headers_cannot_spoof_trusted_iam_context(self):
        self.event["requestContext"]["authorizer"] = {}
        self.event["headers"] = {"authorization": "spoofed", "userArn": "arn:aws:iam::123:user/x"}
        self.assertEqual(self.handle()["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)

    def test_request_from_another_api_is_forbidden(self):
        self.event["requestContext"]["apiId"] = "another-api"
        self.assertEqual(self.handle()["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)

    def test_invalid_uuid_cannot_read_nonrun_or_arbitrary_table_keys(self):
        for value in (None, "", "api#active", "../data", RUN_ID.upper(), 42):
            self.event["pathParameters"]["run_id"] = value
            self.assertEqual(self.handle()["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_wrong_routes_methods_and_payload_versions_are_rejected(self):
        original = deepcopy(self.event)
        self.event["routeKey"] = "POST /runs"
        self.assertEqual(self.handle()["statusCode"], 404)
        self.event = deepcopy(original)
        self.event["requestContext"]["http"]["method"] = "POST"
        self.assertEqual(self.handle()["statusCode"], 405)
        self.event = deepcopy(original)
        self.event["version"] = "1.0"
        self.assertEqual(self.handle()["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_query_cannot_redirect_table_bucket_or_operation(self):
        self.event["queryStringParameters"] = {"bucket": "another", "operation": "train"}
        self.assertEqual(self.handle()["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_unknown_run_returns_404(self):
        self.table.item = None
        self.assertEqual(self.handle()["statusCode"], 404)
        self.assertEqual(self.table.calls, [({"run_id": RUN_ID}, True)])

    def test_backend_error_is_sanitized_without_a_success_result(self):
        def denied(**_kwargs):
            raise RuntimeError("SYNTHETIC PRIVATE SDK DETAILS")
        self.table.get_item = denied
        with self.assertLogs("src.cloud.status_api", level="ERROR") as logs:
            response = self.handle()
        self.assertEqual(response["statusCode"], 503)
        self.assertNotIn("PRIVATE", response["body"])
        self.assertNotIn("PRIVATE", "".join(logs.output))

    def test_unsupported_or_conflicting_records_are_not_presented_as_completed(self):
        original = deepcopy(self.table.item)
        for field, value in (
            ("run_id", "another"), ("registry_version", "unknown"), ("status", "RUNNING"),
            ("operation", "train"), ("live_inference", True), ("live_inference", 0),
        ):
            self.table.item = deepcopy(original)
            self.table.item[field] = value
            with self.assertLogs("src.cloud.status_api", level="ERROR"):
                self.assertEqual(self.handle()["statusCode"], 503)

    def test_invalid_numeric_metadata_is_rejected(self):
        original = deepcopy(self.table.item)
        for value in (Decimal("1.5"), Decimal("NaN"), True, float("inf")):
            self.table.item = deepcopy(original)
            self.table.item["counts"]["train"]["documents"] = value
            with self.assertLogs("src.cloud.status_api", level="ERROR"):
                self.assertEqual(self.handle()["statusCode"], 503)

    def test_incomplete_or_inconsistent_counts_are_rejected(self):
        self.table.item["counts"]["train"]["input_rows"] = Decimal(7190)
        with self.assertLogs("src.cloud.status_api", level="ERROR"):
            self.assertEqual(self.handle()["statusCode"], 503)
        self.table.item["counts"]["train"]["input_rows"] = Decimal(7191)
        del self.table.item["artifacts"]["manifest.json"]["version_id"]
        with self.assertLogs("src.cloud.status_api", level="ERROR"):
            self.assertEqual(self.handle()["statusCode"], 503)

    def test_cloudshell_client_restricts_destinations_before_loading_credentials(self):
        path = Path(__file__).resolve().parents[2] / "setup/call_cpu_status_api.py"
        spec = importlib.util.spec_from_file_location("status_client", path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        url = client.request_url("https://abc123.execute-api.us-east-1.amazonaws.com", RUN_ID)
        self.assertTrue(url.endswith("/runs/" + RUN_ID))
        for endpoint in (
            "http://abc123.execute-api.us-east-1.amazonaws.com",
            "https://example.com", "https://abc123.execute-api.us-east-1.amazonaws.com.evil.example",
            "https://abc123.execute-api.us-east-1.amazonaws.com?x=1",
            "https://user:pass@abc123.execute-api.us-east-1.amazonaws.com",
        ):
            with self.assertRaises(ValueError):
                client.request_url(endpoint, RUN_ID)


if __name__ == "__main__":
    unittest.main()
