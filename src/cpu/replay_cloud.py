"""Finite CodeBuild adapter for a pinned historical benchmark replay.

Uses only current GetObject reads matched to an approved version, scoped writes
under this build's runs/ prefix, and existing bucket configuration permissions.
"""

import argparse
import json
import os
from pathlib import Path
import tempfile

from .catalog_publish import (
    BUCKET, aws_json, destination as catalog_destination, publish_file,
)
from .historical_replay import CONFIG_PATH, OUTPUT_FILES, prepare_replay, sha256
from .prompt import json_bytes
from .source_catalog import parse_json, require


def download_reference(identity, path, aws):
    require(identity.get("bucket") == BUCKET, "Unexpected reference bucket")
    require(isinstance(identity.get("key"), str)
            and identity["key"].startswith("runs/")
            and identity["key"].endswith("/catalog/test.jsonl")
            and ".." not in identity["key"], "Unexpected reference key")
    expected_size = identity.get("bytes")
    require(type(expected_size) is int and 0 < expected_size <= 32 * 1024 * 1024,
            "Reference size is outside limits")
    require(isinstance(identity.get("version_id"), str)
            and identity["version_id"] not in ("", "null"), "An approved reference version is required")
    head = aws("s3api", "head-object", "--bucket", BUCKET, "--key", identity["key"])
    require(head.get("VersionId") == identity["version_id"]
            and head.get("ContentLength") == expected_size
            and head.get("ServerSideEncryption") == "AES256",
            "Reference metadata differs from the accepted catalog")
    response = aws(
        "s3api", "get-object", "--bucket", BUCKET, "--key", identity["key"], str(path)
    )
    require(response.get("VersionId") == identity["version_id"]
            and response.get("ServerSideEncryption") == "AES256",
            "Reference version changed during download")
    body = path.read_bytes()
    require(len(body) == expected_size and sha256(body) == identity["sha256"],
            "Reference checksum differs from the accepted catalog")
    return body


def compact_groups(report):
    result = {}
    for name, group in report["groups"].items():
        request = group["request_level"]
        first = group["unique_documents_first_attempt"]
        result[name] = {
            "requests": request["attempted_documents"],
            "scored_requests": request["scored_documents"],
            "missing_requests": request["missing_documents"],
            "invalid_requests": request["invalid_documents"],
            "unique_reference_documents": group["unique_reference_documents"],
            "unique_input_contracts": group["unique_input_contracts"],
            "matched_reference_records": group["matched_reference_records"],
            "reference_subset_fraction": group["reference_subset_fraction"],
            "request_label_accuracy_all_attempts": request["label_accuracy_all_attempts"],
            "first_attempt_label_accuracy_all_attempts": first["label_accuracy_all_attempts"],
            "recorded_transport_failures": group["failure_accounting"]["recorded_transport_failures"],
        }
    return result


def run_replay(root, source_commit, environ, config, aws=aws_json):
    build_id, catalog_prefix = catalog_destination(environ)
    prefix = catalog_prefix.removesuffix("catalog/") + "evaluation/"
    require((aws("s3api", "get-bucket-location", "--bucket", BUCKET)
             .get("LocationConstraint") or "us-east-1") == "us-east-1",
            "Evaluation bucket is in another region")
    require(aws("s3api", "get-bucket-versioning", "--bucket", BUCKET)
            .get("Status") == "Enabled", "Enable S3 versioning before replay")
    with tempfile.TemporaryDirectory(prefix="legal-cpu-historical-replay-") as workspace:
        directory = Path(workspace)
        reference = download_reference(config["reference"], directory / "test.jsonl", aws)
        output = directory / "prepared"
        report = prepare_replay(root, reference, config, source_commit, output)
        require({path.name for path in output.iterdir()} == set(OUTPUT_FILES),
                "Unexpected replay artifacts")
        readback = directory / "readback"
        readback.mkdir()
        artifacts = {}
        for filename in OUTPUT_FILES:
            artifacts[filename] = publish_file(
                output / filename, prefix + filename, source_commit, readback, aws
            )
        manifest = {
            "schema_version": "contractnli-evaluation-artifacts-v1",
            "operation": report["operation"], "live_inference": False,
            "evaluation_kind": "historical_benchmark_replay",
            "registration_scope": "versioned_s3_manifest",
            "status": "ARTIFACTS_VERIFIED", "required_build_status": "SUCCEEDED",
            "build_id": build_id, "source_commit": source_commit,
            "historical_source_commit": config["historical_source_commit"],
            "bucket": BUCKET, "prefix": prefix, "created_at": report["created_at"],
            "reference": config["reference"], "groups": compact_groups(report),
            "artifacts": artifacts, "limitations": config["limitations"],
        }
        manifest_path = directory / "evaluation.json"
        manifest_path.write_bytes(json_bytes(manifest))
        receipt = publish_file(
            manifest_path, prefix + "evaluation.json", source_commit, readback, aws
        )
    return {
        "operation": manifest["operation"], "evaluation_kind": manifest["evaluation_kind"],
        "live_inference": False, "build_id": build_id, "source_commit": source_commit,
        "bucket": BUCKET, "manifest": receipt, "groups": manifest["groups"],
        "required_build_status": "SUCCEEDED",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config = parse_json(CONFIG_PATH.read_bytes())
    print(json.dumps(run_replay(root, args.source_commit, os.environ, config), indent=2))


if __name__ == "__main__":
    main()
