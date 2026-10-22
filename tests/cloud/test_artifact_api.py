"""Thin metadata API fixtures; execute in CodeBuild, not on the laptop."""

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.cloud.artifact_api import handle_request
from src.cloud.artifact_api_config import download_current, load_build_config, render_policy, validate_config

ROOT = Path(__file__).resolve().parents[2]
API_ID = "fixture-api"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture_documents(approved):
    def common(name):
        identity = approved[name]
        return {
            "schema_version": identity["schema_version"], "operation": identity["operation"],
            "source_commit": identity["source_commit"], "build_id": identity["build_id"],
            "bucket": approved["bucket"], "prefix": identity["prefix"],
            "status": "ARTIFACTS_VERIFIED", "required_build_status": "SUCCEEDED",
            "registration_scope": "versioned_s3_manifest", "live_inference": False,
            "created_at": "2026-10-22T14:03:59+00:00", "artifacts": {},
            "contract": "SYNTHETIC_PRIVATE_BODY",
        }
    def artifact(name, filename, digest="c" * 64):
        return {
            "key": approved[name]["prefix"] + filename,
            "version_id": "fixture-" + filename, "bytes": 1234,
            "sha256": digest, "encryption": "AES256",
            "contract": "SYNTHETIC_PRIVATE_BODY",
        }
    catalog, evaluation = common("catalog"), common("evaluation")
    catalog["upstream"] = {
        "dataset_source": "https://stanfordnlp.github.io/contract-nli/",
        "dataset_license": "CC BY 4.0", "credential": "SYNTHETIC_CREDENTIAL",
    }
    catalog["checks"] = {
        "canonical_hypotheses": True, "expected_counts": True,
        "split_id_and_exact_text_disjoint": True, "tracked_train_validation_semantic_match": True,
    }
    catalog["splits"] = {}
    for split, count in approved["catalog"]["expected_documents"].items():
        processed = artifact("catalog", split + ".jsonl")
        if split == "test":
            processed.update({field: approved["reference_test"][field]
                              for field in ("key", "version_id", "sha256", "bytes")})
        source = artifact("catalog", "source-" + split + ".json")
        catalog["artifacts"][split + ".jsonl"] = processed
        catalog["artifacts"]["source-" + split + ".json"] = source
        catalog["splits"][split] = {
            "documents": count, "input_rows": 17 * count,
            "semantic_sha256": "e" * 64,
            "output_sha256": processed["sha256"], "source_sha256": source["sha256"],
            "text": "SYNTHETIC_PRIVATE_BODY",
        }
    catalog["artifacts"]["prompt.json"] = artifact("catalog", "prompt.json", "d" * 64)
    catalog["prompt"] = {
        "name": "contract_extractor_cpu_v1", "file": "prompt.json", "sha256": "d" * 64,
        "model_binding": None, "content": "SYNTHETIC_PROMPT_CONTENT",
    }
    evaluation.update({
        "evaluation_kind": "historical_benchmark_replay",
        "historical_source_commit": approved["evaluation"]["historical_source_commit"],
        "reference": {"bucket": approved["bucket"], **approved["reference_test"]},
        "groups": {},
        "limitations": ["SYNTHETIC_PRIVATE_BODY"],
    })
    for name, requests, scored, invalid, missing, accuracy in (
        ("4_client", 40, 26, 12, 2, 0.5147058823529411),
        ("7_client", 35, 22, 9, 4, 0.507563025210084),
    ):
        evaluation["groups"][name] = {
            "requests": requests, "scored_requests": scored, "invalid_requests": invalid,
            "missing_requests": missing, "unique_input_contracts": 10,
            "matched_reference_records": 10, "recorded_transport_failures": missing,
            "request_label_accuracy_all_attempts": accuracy,
            "first_attempt_label_accuracy_all_attempts": 0.5,
            "reference_subset_fraction": 10 / 123, "contract": "SYNTHETIC_PRIVATE_BODY",
        }
    for filename in ("report.json", "attempts.jsonl"):
        evaluation["artifacts"][filename] = artifact("evaluation", filename)
    return {"catalog": catalog, "evaluation": evaluation}


class FakeS3:
    def __init__(self, approved, documents):
        self.approved, self.documents = approved, documents
        self.calls, self.bodies = [], []
        self.failure = None

    def get_object(self, *, Bucket, Key, VersionId):
        self.calls.append({"Bucket": Bucket, "Key": Key, "VersionId": VersionId})
        if self.failure == "backend":
            raise RuntimeError("SYNTHETIC_CREDENTIAL PRIVATE SDK DETAILS")
        candidates = [name for name in ("catalog", "evaluation")
                      if self.approved[name]["key"] == Key]
        if len(candidates) != 1:
            raise AssertionError("Only two metadata objects are approved")
        name = candidates[0]
        identity = self.approved[name]
        if Bucket != self.approved["bucket"] or VersionId != identity["version_id"]:
            raise AssertionError("Only exact approved object versions are readable")
        payload = self.payloads[name]
        if self.failure == "truncated":
            payload = payload[:-1]
        elif self.failure == "oversized":
            payload += b"x"
        elif self.failure == "corrupt":
            payload = b"X" + payload[1:]
        body = io.BytesIO(payload)
        self.bodies.append(body)
        result = {
            "Body": body, "VersionId": identity["version_id"],
            "ContentLength": identity["bytes"], "ServerSideEncryption": "AES256",
            "Metadata": {"source-commit": identity["source_commit"], "sha256": identity["sha256"]},
        }
        if self.failure == "version":
            result["VersionId"] = "unapproved-version"
        elif self.failure == "encryption":
            result["ServerSideEncryption"] = "aws:kms"
        elif self.failure == "length":
            result["ContentLength"] += 1
        elif self.failure == "metadata":
            result["Metadata"]["sha256"] = "0" * 64
        elif self.failure == "source-metadata":
            result["Metadata"]["source-commit"] = "0" * 40
        return result

    def refresh(self):
        self.payloads = {}
        for name, document in self.documents.items():
            body = json.dumps(document, sort_keys=True).encode("utf-8")
            self.payloads[name] = body
            self.approved[name]["bytes"] = len(body)
            self.approved[name]["sha256"] = hashlib.sha256(body).hexdigest()

    def raw_payload(self, name, body):
        self.payloads[name] = body
        self.approved[name]["bytes"] = len(body)
        self.approved[name]["sha256"] = hashlib.sha256(body).hexdigest()


class ArtifactApiTests(unittest.TestCase):
    def setUp(self):
        self.approved = json.loads((ROOT / "config/approved-artifacts.example.json").read_text())
        self.approved["bucket"] = "fixture-artifact-bucket"
        for name, uuid, commit in (
            ("catalog", "00000000-0000-4000-8000-000000000001", "a" * 40),
            ("evaluation", "00000000-0000-4000-8000-000000000002", "b" * 40),
        ):
            identity = self.approved[name]
            identity.update({
                "prefix": "runs/" + uuid + "/" + name + "/",
                "key": "runs/" + uuid + "/" + name + "/" + name + ".json",
                "version_id": "fixture-" + name + "-version", "source_commit": commit,
                "build_id": "legal-llm-cpu-check:" + uuid, "sha256": "c" * 64,
            })
            if name == "evaluation":
                identity["evaluation_id"] = uuid
                identity["historical_source_commit"] = "d" * 40
        self.approved["reference_test"].update({
            "key": self.approved["catalog"]["prefix"] + "test.jsonl",
            "version_id": "fixture-test-version", "sha256": "e" * 64, "bytes": 1234,
        })
        self.documents = fixture_documents(self.approved)
        self.s3 = FakeS3(self.approved, self.documents)
        self.s3.refresh()
        self.factory_calls = 0
        self.event = {
            "version": "2.0", "routeKey": "GET /datasets",
            "requestContext": {
                "apiId": API_ID, "http": {"method": "GET"},
                "authorizer": {"iam": {
                    "userArn": "arn:aws:iam::000000000000:user/fixture",
                    "accessKey": "SYNTHETIC_CREDENTIAL",
                }},
            },
        }

    def handle(self, route=None):
        if route:
            self.event["routeKey"] = route
        if self.event.get("routeKey") == "GET /evaluations/{evaluation_id}":
            self.event.setdefault("pathParameters", {
                "evaluation_id": self.approved["evaluation"]["evaluation_id"],
            })
        def factory():
            self.factory_calls += 1
            return self.s3
        return handle_request(
            self.event, expected_api_id=API_ID, approved=self.approved, s3_factory=factory
        )

    def assert_unavailable(self, route=None):
        with self.assertLogs("src.cloud.artifact_api", level="ERROR") as logs:
            response = self.handle(route)
        self.assertEqual(response["statusCode"], 503)
        self.assertNotIn("SYNTHETIC", response["body"])
        self.assertNotIn("SYNTHETIC", "".join(logs.output))
        self.assertTrue(all(body.closed for body in self.s3.bodies))

    def test_dataset_metadata_has_three_splits_counts_and_exact_reference_versions(self):
        response = self.handle()
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual([row["documents"] for row in body["datasets"]], [423, 61, 123])
        self.assertEqual(body["datasets"][-1]["processed_artifact"]["version_id"],
                         self.approved["reference_test"]["version_id"])
        self.assertTrue(body["datasets"][-1]["held_out"])
        self.assertEqual(self.s3.calls, [{
            "Bucket": self.approved["bucket"], "Key": self.approved["catalog"]["key"],
            "VersionId": self.approved["catalog"]["version_id"],
        }])
        self.assertTrue(all(stream.closed for stream in self.s3.bodies))
        self.assertEqual(response["headers"]["cache-control"], "no-store")

    def test_prompt_metadata_exposes_identity_without_prompt_content_or_model_binding(self):
        response = self.handle("GET /prompts")
        self.assertEqual(response["statusCode"], 200)
        prompt = json.loads(response["body"])["prompts"][0]
        self.assertEqual(prompt["name"], "contract_extractor_cpu_v1")
        self.assertIsNone(prompt["model_binding"])
        self.assertEqual(prompt["sha256"], prompt["artifact"]["sha256"])
        self.assertNotIn("SYNTHETIC_PROMPT_CONTENT", response["body"])

    def test_evaluation_returns_historical_scope_accounting_coverage_and_report_references(self):
        response = self.handle("GET /evaluations/{evaluation_id}")
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["evaluation_kind"], "historical_benchmark_replay")
        self.assertFalse(body["live_inference"])
        self.assertEqual(body["groups"]["4_client"]["invalid_requests"], 12)
        self.assertEqual(body["groups"]["7_client"]["missing_requests"], 4)
        self.assertEqual(body["reference"]["documents"], 123)
        self.assertEqual(set(body["artifacts"]), {"report.json", "attempts.jsonl"})
        self.assertTrue(any("revisions" in note for note in body["limitations"]))
        self.assertEqual(len(self.s3.calls), 1)
        self.assertEqual(self.s3.calls[0]["Key"], self.approved["evaluation"]["key"])

    def test_allowlisted_responses_do_not_leak_bodies_credentials_or_unknown_fields(self):
        for route in ("GET /datasets", "GET /prompts", "GET /evaluations/{evaluation_id}"):
            body = self.handle(route)["body"]
            self.assertNotIn("SYNTHETIC", body)
            self.assertNotIn("userArn", body)
            self.assertNotIn("accessKey", body)
            self.assertNotIn('"contract"', body)

    def test_missing_iam_or_spoofed_header_identity_is_forbidden_before_s3(self):
        for authorizer in (None, {}, {"iam": {}}, {"jwt": {"userArn": "spoofed"}}):
            self.event["requestContext"]["authorizer"] = authorizer
            self.event["headers"] = {"userArn": "arn:aws:iam::000000000000:user/spoofed"}
            self.assertEqual(self.handle()["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)

    def test_other_api_id_is_forbidden_before_s3(self):
        self.event["requestContext"]["apiId"] = "another"
        self.assertEqual(self.handle()["statusCode"], 403)
        self.assertEqual(self.factory_calls, 0)

    def test_wrong_version_route_and_method_are_rejected_without_s3(self):
        original = deepcopy(self.event)
        for field, value, status in (("version", "1.0", 400), ("routeKey", "POST /runs", 404),
                                     ("routeKey", ["GET /datasets"], 404)):
            self.event = deepcopy(original)
            self.event[field] = value
            self.assertEqual(self.handle()["statusCode"], status)
        self.event = deepcopy(original)
        self.event["requestContext"]["http"]["method"] = "POST"
        self.assertEqual(self.handle()["statusCode"], 405)
        self.assertEqual(self.factory_calls, 0)

    def test_unknown_evaluation_and_invalid_ids_are_rejected_before_s3(self):
        self.event["routeKey"] = "GET /evaluations/{evaluation_id}"
        self.event["pathParameters"] = {"evaluation_id": "00000000-0000-4000-8000-000000000001"}
        self.assertEqual(self.handle()["statusCode"], 404)
        for value in (None, "../source", "abc", 42, "AAAAAAAA-0000-4000-8000-000000000002"):
            self.event["pathParameters"] = {"evaluation_id": value}
            self.assertEqual(self.handle()["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_queries_and_bodies_cannot_redirect_objects_or_start_work(self):
        original = deepcopy(self.event)
        for field, value in (
            ("queryStringParameters", {"key": "train.jsonl", "operation": "train"}),
            ("rawQueryString", "key=train.jsonl"), ("body", '{"operation":"start_build"}'),
        ):
            self.event = deepcopy(original)
            self.event[field] = value
            self.assertEqual(self.handle()["statusCode"], 400)
        self.assertEqual(self.factory_calls, 0)

    def test_backend_exception_is_sanitized(self):
        self.s3.failure = "backend"
        self.assert_unavailable()

    def test_wrong_version_encryption_length_or_metadata_fail_and_close_stream(self):
        for failure in ("version", "encryption", "length", "metadata", "source-metadata"):
            self.s3.failure = failure
            self.assert_unavailable()

    def test_truncated_oversized_or_changed_manifest_bytes_fail_and_close_stream(self):
        for failure in ("truncated", "oversized", "corrupt"):
            self.s3.failure = failure
            self.assert_unavailable()

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        for body in (b'{"x":1,"x":2}', b'{"number":NaN}'):
            self.s3.raw_payload("catalog", body)
            self.assert_unavailable()

    def test_failed_or_conflicting_producer_identity_is_not_exposed_as_success(self):
        original = deepcopy(self.documents["catalog"])
        for field, value in (
            ("status", "RUNNING"), ("operation", "train"), ("live_inference", True),
            ("source_commit", "0" * 40), ("build_id", "another-build"),
            ("prefix", "other/"), ("bucket", "other-bucket"), ("required_build_status", "FAILED"),
            ("schema_version", "unknown"), ("registration_scope", "unknown"),
        ):
            self.documents["catalog"] = deepcopy(original)
            self.documents["catalog"][field] = value
            self.s3.refresh()
            self.assert_unavailable()

    def test_dataset_counts_and_validation_markers_must_be_complete(self):
        original = deepcopy(self.documents["catalog"])
        for mutate in (
            lambda doc: doc["splits"]["train"].update(input_rows=1),
            lambda doc: doc["splits"]["test"].update(documents=True),
            lambda doc: doc["checks"].update(expected_counts=False),
            lambda doc: doc["checks"].update(extra=True),
        ):
            self.documents["catalog"] = deepcopy(original)
            mutate(self.documents["catalog"])
            self.s3.refresh()
            self.assert_unavailable()

    def test_artifact_reference_rejects_other_keys_null_versions_bad_hashes_and_lengths(self):
        original = deepcopy(self.documents["catalog"])
        for field, value in (
            ("key", "arbitrary/private.txt"), ("version_id", "null"), ("sha256", "invalid"),
            ("encryption", "aws:kms"), ("bytes", True), ("bytes", 0),
        ):
            self.documents["catalog"] = deepcopy(original)
            self.documents["catalog"]["artifacts"]["train.jsonl"][field] = value
            self.s3.refresh()
            self.assert_unavailable()
        self.assertTrue(all(call["Key"] == self.approved["catalog"]["key"] for call in self.s3.calls))

    def test_prompt_digest_name_and_binding_must_match_registered_artifact(self):
        original = deepcopy(self.documents["catalog"])
        for field, value in (("sha256", "0" * 64), ("name", "unknown"), ("model_binding", "model")):
            self.documents["catalog"] = deepcopy(original)
            self.documents["catalog"]["prompt"][field] = value
            self.s3.refresh()
            self.assert_unavailable("GET /prompts")

    def test_evaluation_rejects_bad_denominators_numeric_values_and_coverage(self):
        original = deepcopy(self.documents["evaluation"])
        for field, value in (
            ("requests", 999), ("invalid_requests", -1),
            ("request_label_accuracy_all_attempts", True),
            ("request_label_accuracy_all_attempts", 1.5),
            ("first_attempt_label_accuracy_all_attempts", float("inf")),
            ("reference_subset_fraction", 0.5),
            ("unique_input_contracts", 0), ("matched_reference_records", 124),
        ):
            self.documents["evaluation"] = deepcopy(original)
            self.documents["evaluation"]["groups"]["4_client"][field] = value
            self.s3.refresh()
            self.assert_unavailable("GET /evaluations/{evaluation_id}")

    def test_evaluation_rejects_a_substituted_reference_or_generating_source_claim(self):
        original = deepcopy(self.documents["evaluation"])
        for mutate in (
            lambda doc: doc["reference"].update(version_id="other"),
            lambda doc: doc["reference"].update(sha256="0" * 64),
            lambda doc: doc.update(historical_source_commit="0" * 40),
            lambda doc: doc.update(evaluation_kind="new_model_run"),
        ):
            self.documents["evaluation"] = deepcopy(original)
            mutate(self.documents["evaluation"])
            self.s3.refresh()
            self.assert_unavailable("GET /evaluations/{evaluation_id}")

    def test_cloudshell_client_preserves_run_route_and_restricts_metadata_paths(self):
        client = load_script("artifact_client_fixture", ROOT / "setup/call_cpu_status_api.py")
        endpoint = "https://fixtureapi.execute-api.us-east-1.amazonaws.com"
        evaluation_id = self.approved["evaluation"]["evaluation_id"]
        self.assertTrue(client.request_url(endpoint, evaluation_id).endswith("/runs/" + evaluation_id))
        self.assertEqual(client.artifact_request_url(endpoint, "datasets"), endpoint + "/datasets")
        self.assertTrue(client.artifact_request_url(endpoint, "evaluation", evaluation_id)
                        .endswith("/evaluations/" + evaluation_id))
        for resource, identifier in (("train", None), ("datasets", evaluation_id),
                                     ("evaluation", "../private"), ("evaluation", None)):
            with self.assertRaises((ValueError, TypeError, AttributeError)):
                client.artifact_request_url(endpoint, resource, identifier)
        for endpoint in ("https://example.com", "http://fixtureapi.execute-api.us-east-1.amazonaws.com",
                         "https://fixtureapi.execute-api.us-east-1.amazonaws.com?key=secret"):
            with self.assertRaises(ValueError):
                client.artifact_request_url(endpoint, "prompts")

    def test_packages_remain_standalone_with_exact_allowlisted_members(self):
        packager = load_script("artifact_packager_fixture", ROOT / "setup/package_registry.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for component, expected in (
                ("registry", {"registry.py"}), ("status-api", {"status_api.py"}),
                ("artifact-api", {"artifact_api.py", "approved_artifacts.json"}),
            ):
                path = root / (component + ".zip")
                private_path = root / "approved.json"
                private_path.write_text(json.dumps(self.approved))
                packager.create_package(
                    component, path, approved_path=private_path if component == "artifact-api" else None
                )
                with zipfile.ZipFile(path) as archive:
                    self.assertEqual(set(archive.namelist()), expected)
                    if component == "artifact-api":
                        extracted = root / "lambda"
                        archive.extractall(extracted)
                        standalone = load_script("standalone_artifact_fixture", extracted / "artifact_api.py")
                        approved = json.loads((extracted / "approved_artifacts.json").read_text())
                        response = standalone.handle_request(
                            {}, expected_api_id=API_ID, approved=approved,
                            s3_factory=lambda: self.fail("Unauthorized request instantiated S3"),
                        )
                        self.assertEqual(response["statusCode"], 403)
            with self.assertRaises(ValueError):
                packager.create_package("../arbitrary", root / "rejected.zip")

    def test_private_configuration_requires_resolved_exact_producer_identities(self):
        self.assertIs(validate_config(self.approved), self.approved)
        original = deepcopy(self.approved)
        for name, field, value in (
            ("catalog", "key", "runs/other/catalog/catalog.json"),
            ("catalog", "source_commit", "__SOURCE_COMMIT__"),
            ("evaluation", "version_id", "null"),
            ("evaluation", "evaluation_id", "00000000-0000-4000-8000-000000000003"),
            ("reference_test", "key", "arbitrary/test.jsonl"),
        ):
            rejected = deepcopy(original)
            rejected[name][field] = value
            with self.assertRaises(ValueError):
                validate_config(rejected)
        with self.assertRaises(ValueError):
            validate_config(original, bucket="different-bucket")
        unresolved = json.loads((ROOT / "config/approved-artifacts.example.json").read_text())
        with self.assertRaises(ValueError):
            validate_config(unresolved)

    def test_private_config_download_checks_actual_version_encryption_size_and_bytes(self):
        payload = b'{"private":"synthetic"}'
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(payload)
            good = {"VersionId": "fixture-version", "ServerSideEncryption": "AES256",
                    "ContentLength": len(payload)}
            with patch("src.cloud.artifact_api_config.aws_json", return_value=good):
                self.assertEqual(download_current(
                    "fixture-bucket", "fixture-key", "fixture-version", digest, path
                )[1], payload)
            for field, value in (("VersionId", "another"), ("ServerSideEncryption", "aws:kms"),
                                 ("ContentLength", len(payload) + 1)):
                bad = {**good, field: value}
                with patch("src.cloud.artifact_api_config.aws_json", return_value=bad):
                    with self.assertRaises(ValueError):
                        download_current("fixture-bucket", "fixture-key", "fixture-version", digest, path)
            with patch("src.cloud.artifact_api_config.aws_json", return_value=good):
                with self.assertRaises(ValueError):
                    download_current("fixture-bucket", "fixture-key", "fixture-version", "0" * 64, path)

    def test_build_private_configuration_requires_pinned_receipt_and_project(self):
        body = json.dumps(self.approved).encode()
        digest = hashlib.sha256(body).hexdigest()
        environment = {
            "OUTPUT_BUCKET": self.approved["bucket"], "AWS_DEFAULT_REGION": "us-east-1",
            "CODEBUILD_BUILD_ID": "legal-llm-cpu-check:00000000-0000-4000-8000-000000000003",
            "APPROVED_ARTIFACT_CONFIG_KEY":
                "runs/00000000-0000-4000-8000-000000000003/config/approved-artifacts.json",
            "APPROVED_ARTIFACT_CONFIG_VERSION": "private-config-version",
            "APPROVED_ARTIFACT_CONFIG_SHA256": digest,
        }
        metadata = {"Metadata": {"sha256": digest}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            with patch.dict("os.environ", environment, clear=True):
                with patch("src.cloud.artifact_api_config.download_current",
                           return_value=(metadata, body)) as download:
                    self.assertEqual(load_build_config(path), self.approved)
                    self.assertEqual(path.read_bytes(), body)
                    self.assertEqual(download.call_args.args[:4], (
                        self.approved["bucket"], environment["APPROVED_ARTIFACT_CONFIG_KEY"],
                        "private-config-version", digest,
                    ))
            environment["CODEBUILD_BUILD_ID"] = "another-project:fixture"
            with patch.dict("os.environ", environment, clear=True):
                with patch("src.cloud.artifact_api_config.download_current") as download:
                    with self.assertRaises(ValueError):
                        load_build_config(Path(directory) / "rejected.json")
                    download.assert_not_called()

    def test_packager_requires_explicit_private_config_for_only_artifact_profile(self):
        packager = load_script("private_packager_fixture", ROOT / "setup/package_registry.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejected.zip"
            with self.assertRaises(ValueError):
                packager.create_package("artifact-api", path)
            with self.assertRaises(ValueError):
                packager.create_package("registry", path, approved_path="unread-private.json")

    def test_runtime_policy_allows_only_two_exact_object_versions_and_own_logs(self):
        template = json.loads((ROOT / "infra/serverless-demo/artifact-api/execution-policy.template.json")
                              .read_text())
        policy = render_policy(self.approved, "000000000000", template)
        statements = policy["Statement"]
        s3_statements = [item for item in statements if item.get("Action") == "s3:GetObjectVersion"]
        self.assertEqual(len(s3_statements), 2)
        expected = {
            "arn:aws:s3:::" + self.approved["bucket"] + "/" + self.approved[name]["key"]:
                self.approved[name]["version_id"] for name in ("catalog", "evaluation")
        }
        actual = {item["Resource"]: item["Condition"]["StringEquals"]["s3:VersionId"]
                  for item in s3_statements}
        self.assertEqual(actual, expected)
        self.assertTrue(all("*" not in item["Resource"] for item in s3_statements))
        self.assertEqual({action for item in statements for action in (
            item["Action"] if isinstance(item["Action"], list) else [item["Action"]]
        )}, {"s3:GetObjectVersion", "logs:CreateLogStream", "logs:PutLogEvents"})


if __name__ == "__main__":
    unittest.main()
