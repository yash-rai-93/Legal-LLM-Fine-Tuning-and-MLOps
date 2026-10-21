"""Registry contract/failure tests. All bodies are synthetic metadata fixtures.

Run in AWS CodeBuild, never as evidence of model inference or accuracy.
"""
from copy import deepcopy
import hashlib
import io
import json
import unittest

from src.cloud.registry import MAX_DATA_BYTES, read_versioned_object, register_run

PROJECT = "legal-llm-cpu-check"
BUCKET = "fixture-private-bucket"
PROCESSOR = "a" * 40
REGISTRY = "b" * 40
RUN_ID = "12345678-1234-4234-8234-123456789012"
BUILD_ID = PROJECT + ":" + RUN_ID
PREFIX = "runs/" + RUN_ID + "/"


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.head_requests, self.get_requests = [], []
        self.size_override, self.response_version_override = None, None

    def add(self, name, payload, version=None, encryption="AES256"):
        key = PREFIX + name
        self.objects[key] = {
            "payload": payload, "version": version or "version-" + name,
            "encryption": encryption,
        }

    def head_object(self, *, Bucket, Key):
        self.head_requests.append((Bucket, Key))
        entry = self.objects[Key]
        return {
            "VersionId": entry["version"],
            "ContentLength": len(entry["payload"]) if self.size_override is None else self.size_override,
            "ServerSideEncryption": entry["encryption"],
        }

    def get_object(self, *, Bucket, Key, VersionId):
        self.get_requests.append((Bucket, Key, VersionId))
        entry = self.objects[Key]
        if VersionId != entry["version"]:
            raise KeyError("Unknown fixture version")
        return {
            "VersionId": self.response_version_override or VersionId,
            "Body": io.BytesIO(entry["payload"]),
        }


class FakeCodeBuild:
    def __init__(self):
        self.calls = []
        self.build = {
            "id": BUILD_ID, "buildStatus": "SUCCEEDED", "buildComplete": True,
            "resolvedSourceVersion": PROCESSOR,
            "source": {
                "type": "GITHUB", "buildspec": "buildspecs/cpu-publish.yml",
                "location": "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps.git",
            },
            "environment": {"environmentVariables": [{"name": "OUTPUT_BUCKET", "value": BUCKET}]},
        }
        self.not_found = False

    def batch_get_builds(self, *, ids):
        self.calls.append(ids)
        if self.not_found:
            return {"builds": [], "buildsNotFound": [BUILD_ID]}
        return {"builds": [deepcopy(self.build)]}


class FakeTable:
    def __init__(self):
        self.item, self.writes, self.reads = None, 0, []

    def put_item(self, *, Item, ConditionExpression):
        if ConditionExpression != "attribute_not_exists(run_id)":
            raise AssertionError("Run creation must be conditional")
        if self.item is not None:
            raise AwsError("ConditionalCheckFailedException")
        self.item, self.writes = deepcopy(Item), self.writes + 1

    def get_item(self, *, Key, ConsistentRead):
        self.reads.append((Key, ConsistentRead))
        return {"Item": deepcopy(self.item)}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.s3, self.codebuild, self.table = FakeS3(), FakeCodeBuild(), FakeTable()
        self.manifest = {
            "schema_version": "contractnli-cpu-v1", "operation": "data_preparation",
            "live_inference": False, "source_commit": PROCESSOR,
            "created_at": "2026-10-21T22:39:44+00:00",
            "dataset_source": "https://stanfordnlp.github.io/contract-nli/",
            "dataset_license": "CC BY 4.0", "splits": {},
        }
        for split in ("train", "validation"):
            # These bytes exercise hashes only, not the ContractNLI row validator.
            payload = json.dumps({"fixture": "synthetic-" + split}).encode() + b"\n"
            self.s3.add(split + ".jsonl", payload)
            self.manifest["splits"][split] = {
                "input_rows": 17, "documents": 1, "input_sha256": "1" * 64,
                "semantic_sha256": "2" * 64,
                "output_sha256": hashlib.sha256(payload).hexdigest(),
            }
        self.save_manifest()
        self.event = {
            "operation": "data_preparation", "live_inference": False,
            "bucket": BUCKET, "source_commit": PROCESSOR, "build_id": BUILD_ID,
            "build_status": "SUCCEEDED", "prefix": PREFIX,
        }

    def save_manifest(self):
        self.s3.add("manifest.json", json.dumps(self.manifest).encode())

    def run_registry(self):
        return register_run(
            self.event, s3=self.s3, codebuild=self.codebuild, table=self.table,
            bucket=BUCKET, project=PROJECT, processor_commit=PROCESSOR, registry_commit=REGISTRY,
        )

    def test_success_records_all_versions_and_metadata_without_bodies(self):
        result = self.run_registry()
        self.assertEqual((result["status"], result["created"]), ("COMPLETED", True))
        self.assertEqual(result["counts"]["train"]["documents"], 1)
        self.assertEqual(self.codebuild.calls, [[BUILD_ID]])
        self.assertEqual(self.table.writes, 1)
        for name in ("manifest.json", "train.jsonl", "validation.jsonl"):
            entry = self.table.item["artifacts"][name]
            self.assertEqual(entry["version_id"], "version-" + name)
            self.assertIn((BUCKET, PREFIX + name, "version-" + name), self.s3.get_requests)
        self.assertNotIn("synthetic-train", json.dumps(self.table.item))
        self.assertFalse(result["live_inference"])

    def test_duplicate_returns_original_record_without_overwrite(self):
        first = self.run_registry()
        item = deepcopy(self.table.item)
        second = self.run_registry()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["registered_at"], second["registered_at"])
        self.assertEqual(self.table.item, item)
        self.assertEqual(self.table.writes, 1)
        self.assertEqual(self.table.reads, [({"run_id": RUN_ID}, True)])

    def test_event_cannot_redirect_bucket_project_prefix_or_commit(self):
        for field, value in (
            ("bucket", "another-bucket"), ("prefix", "runs/../another/"),
            ("build_id", "gpu-project:" + RUN_ID), ("source_commit", "c" * 40),
            ("build_status", "RUNNING"), ("live_inference", True),
            ("live_inference", 0), ("operation", "train"),
            ("build_id", PROJECT + ":invalid"),
        ):
            with self.subTest(field=field, value=value):
                original = deepcopy(self.event)
                self.event[field] = value
                with self.assertRaises(ValueError):
                    self.run_registry()
                self.event = original
        self.assertEqual(self.codebuild.calls, [])
        self.assertEqual(self.s3.head_requests, [])
        self.assertEqual(self.table.writes, 0)

    def test_real_build_must_confirm_success_commit_repository_and_bucket(self):
        for field, value in (
            ("buildStatus", "FAILED"), ("buildComplete", False),
            ("resolvedSourceVersion", "c" * 40), ("id", "unexpected"),
            ("source", {"type": "GITHUB", "location": "https://example.com/repo"}),
            ("source", {"type": "GITHUB", "location": "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps", "buildspec": "unapproved.yml"}),
            ("environment", {"environmentVariables": []}),
        ):
            with self.subTest(field=field):
                original = deepcopy(self.codebuild.build)
                self.codebuild.build[field] = value
                with self.assertRaises(ValueError):
                    self.run_registry()
                self.codebuild.build = original
        self.assertEqual(self.s3.head_requests, [])
        self.assertEqual(self.table.writes, 0)

    def test_unavailable_build_cannot_be_registered(self):
        self.codebuild.not_found = True
        with self.assertRaisesRegex(ValueError, "not found"):
            self.run_registry()
        self.assertEqual(self.s3.head_requests, [])

    def test_modified_output_bytes_prevent_registration(self):
        self.s3.objects[PREFIX + "train.jsonl"]["payload"] += b"corruption"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.run_registry()
        self.assertEqual(self.table.writes, 0)

    def test_missing_manifest_or_output_prevents_registration(self):
        for name in ("manifest.json", "train.jsonl", "validation.jsonl"):
            with self.subTest(name=name):
                old = self.s3.objects.pop(PREFIX + name)
                with self.assertRaises(KeyError):
                    self.run_registry()
                self.s3.objects[PREFIX + name] = old
        self.assertEqual(self.table.writes, 0)

    def test_null_versions_and_unexpected_encryption_are_rejected(self):
        entry = self.s3.objects[PREFIX + "manifest.json"]
        for field, value in (("version", "null"), ("version", ""), ("encryption", "aws:kms")):
            with self.subTest(field=field, value=value):
                original = entry[field]
                entry[field] = value
                with self.assertRaises(ValueError):
                    self.run_registry()
                entry[field] = original
        self.assertEqual(self.table.writes, 0)

    def test_invalid_manifest_schema_provenance_counts_and_hashes_are_rejected(self):
        cases = [
            ("schema_version", "unknown"), ("live_inference", True),
            ("source_commit", "c" * 40), ("dataset_license", "unknown"),
            ("created_at", "2026-10-21T10:00:00"),
            ("splits", {"train": self.manifest["splits"]["train"]}),
        ]
        original = deepcopy(self.manifest)
        for field, value in cases:
            with self.subTest(field=field):
                self.manifest = deepcopy(original)
                self.manifest[field] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.run_registry()
        for field, value in (("documents", True), ("input_rows", 16), ("output_sha256", "bad")):
            with self.subTest(split_field=field):
                self.manifest = deepcopy(original)
                self.manifest["splits"]["train"][field] = value
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.run_registry()
        self.assertEqual(self.table.writes, 0)

    def test_malformed_and_duplicate_key_manifests_are_rejected(self):
        for payload in (b"{", b'{"x":1,"x":2}', b'{"x":NaN}'):
            self.s3.add("manifest.json", payload)
            with self.assertRaisesRegex(ValueError, "Invalid manifest JSON"):
                self.run_registry()
        self.assertEqual(self.table.writes, 0)

    def test_conflicting_existing_run_is_not_overwritten(self):
        self.run_registry()
        self.table.item["fingerprint"] = "conflict"
        original = deepcopy(self.table.item)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.run_registry()
        self.assertEqual(self.table.item, original)
        self.assertEqual(self.table.writes, 1)

    def test_permission_error_is_not_treated_as_an_idempotent_success(self):
        def denied(**_kwargs):
            raise AwsError("AccessDeniedException")
        self.table.put_item = denied
        with self.assertRaises(AwsError):
            self.run_registry()

    def test_streamed_hash_reads_exact_version_without_buffering_data(self):
        payload = b"metadata-only-fixture\n" * 20000
        self.s3.add("large.jsonl", payload)
        metadata, captured = read_versioned_object(
            self.s3, BUCKET, PREFIX + "large.jsonl", max_bytes=MAX_DATA_BYTES
        )
        self.assertEqual(metadata["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertIsNone(captured)
        self.assertEqual(self.s3.get_requests[-1][2], "version-large.jsonl")

    def test_size_limit_truncation_and_wrong_version_are_errors(self):
        key = PREFIX + "train.jsonl"
        self.s3.size_override = MAX_DATA_BYTES + 1
        with self.assertRaisesRegex(ValueError, "size"):
            read_versioned_object(self.s3, BUCKET, key, max_bytes=MAX_DATA_BYTES)
        self.assertEqual(self.s3.get_requests, [])
        self.s3.size_override = len(self.s3.objects[key]["payload"]) + 1
        with self.assertRaisesRegex(ValueError, "truncated"):
            read_versioned_object(self.s3, BUCKET, key, max_bytes=MAX_DATA_BYTES)
        self.s3.size_override = None
        self.s3.response_version_override = "different-version"
        with self.assertRaisesRegex(ValueError, "different object version"):
            read_versioned_object(self.s3, BUCKET, key, max_bytes=MAX_DATA_BYTES)


if __name__ == "__main__":
    unittest.main()
