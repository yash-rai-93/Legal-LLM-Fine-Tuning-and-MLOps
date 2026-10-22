"""Publish a verified dataset/prompt catalog under the current CodeBuild run.

No resource creation or DynamoDB updates. A catalog is consumable only after
this exact build succeeds; the catalog describes verified S3 artifacts.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from .prompt import build_prompt, json_bytes
from .source_catalog import ARTIFACT_NAMES, parse_json, require

BUCKET = "yash-legal-llm-cpu-20261022"
PROJECT = "legal-llm-cpu-check"


def destination(environ):
    require(environ.get("AWS_DEFAULT_REGION") == "us-east-1", "Unexpected AWS region")
    require(environ.get("OUTPUT_BUCKET") == BUCKET, "Unexpected artifact bucket")
    build_id = environ.get("CODEBUILD_BUILD_ID", "")
    project, separator, run_id = build_id.partition(":")
    require(separator and project == PROJECT, "Unexpected build project")
    try:
        require(str(uuid.UUID(run_id)) == run_id, "Invalid build UUID")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid build UUID") from exc
    return build_id, "runs/" + run_id + "/catalog/"


def aws_json(*arguments):
    result = subprocess.run(
        ["aws", *arguments, "--region", "us-east-1", "--output", "json", "--no-cli-pager"],
        capture_output=True, text=True, timeout=90, check=False,
    )
    if result.returncode:
        raise RuntimeError("AWS catalog request failed: " + "/".join(arguments[:2]))
    return json.loads(result.stdout) if result.stdout.strip() else {}


def validate_prepared(directory, source_commit):
    require(isinstance(source_commit, str) and re.fullmatch(r"[0-9a-f]{40}", source_commit),
            "An immutable catalog commit is required")
    directory = Path(directory)
    require({path.name for path in directory.iterdir()} == set(ARTIFACT_NAMES),
            "Unexpected or missing prepared files")
    for name in ARTIFACT_NAMES:
        path = directory / name
        require(path.is_file() and not path.is_symlink()
                and 0 < path.stat().st_size <= 32 * 1024 * 1024,
                "Prepared file outside size/type limits")
    manifest = parse_json((directory / "preparation.json").read_bytes())
    require(manifest.get("schema_version") == "contractnli-catalog-preparation-v1"
            and manifest.get("operation") == "dataset_prompt_preparation"
            and manifest.get("live_inference") is False
            and manifest.get("source_commit") == source_commit,
            "Unexpected preparation identity")
    expected_names = set(ARTIFACT_NAMES) - {"preparation.json"}
    require(set(manifest.get("artifact_sha256", {})) == expected_names,
            "Incomplete preparation checksums")
    for name, digest in manifest["artifact_sha256"].items():
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest,
                "Prepared bytes changed: " + name)
    require((directory / "prompt.json").read_bytes() == json_bytes(build_prompt()),
            "Prompt differs from the canonical prompt/schema")
    require(manifest.get("prompt", {}).get("sha256")
            == manifest["artifact_sha256"]["prompt.json"], "Prompt identity mismatch")
    splits = manifest.get("splits", {})
    require(set(splits) == {"train", "validation", "test"}, "Incomplete dataset splits")
    for split, counts in splits.items():
        require(type(counts.get("documents")) is int and counts["documents"] > 0
                and type(counts.get("input_rows")) is int
                and counts["input_rows"] == 17 * counts["documents"],
                "Invalid prepared document counts")
        require(counts.get("output_sha256") == manifest["artifact_sha256"][split + ".jsonl"]
                and counts.get("source_sha256")
                == manifest["artifact_sha256"]["source-" + split + ".json"],
                "Split artifact checksum mismatch")
    require(manifest.get("checks") == {
        "expected_counts": True, "canonical_hypotheses": True,
        "split_id_and_exact_text_disjoint": True,
        "tracked_train_validation_semantic_match": True,
    }, "Preparation checks are incomplete")
    return manifest


def publish_file(path, key, source_commit, verification_dir, aws):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    uploaded = aws(
        "s3api", "put-object", "--bucket", BUCKET, "--key", key,
        "--body", str(path), "--server-side-encryption", "AES256",
        "--if-none-match", "*",
        "--metadata", json.dumps({"source-commit": source_commit, "sha256": digest}),
    )
    version = uploaded.get("VersionId")
    require(isinstance(version, str) and version not in ("", "null"),
            "S3 did not assign a version")
    verified_path = verification_dir / path.name
    # Existing build permissions allow current GetObject, not GetObjectVersion.
    # Match the returned version to the just-uploaded version before accepting bytes.
    downloaded = aws(
        "s3api", "get-object", "--bucket", BUCKET, "--key", key, str(verified_path)
    )
    require(
        downloaded.get("VersionId") == version
        and downloaded.get("ServerSideEncryption") == "AES256"
        and hashlib.sha256(verified_path.read_bytes()).hexdigest() == digest,
        "S3 read-back version, encryption or checksum mismatch",
    )
    return {
        "key": key, "version_id": version, "sha256": digest,
        "bytes": path.stat().st_size, "encryption": "AES256",
    }


def publish_catalog(directory, source_commit, environ, aws=aws_json):
    build_id, prefix = destination(environ)
    manifest = validate_prepared(directory, source_commit)
    require((aws("s3api", "get-bucket-location", "--bucket", BUCKET)
             .get("LocationConstraint") or "us-east-1") == "us-east-1",
            "Artifact bucket is in another region")
    require(aws("s3api", "get-bucket-versioning", "--bucket", BUCKET)
            .get("Status") == "Enabled", "S3 versioning must be enabled")
    artifacts = {}
    with tempfile.TemporaryDirectory(prefix="legal-cpu-catalog-publish-") as workspace:
        verification_dir = Path(workspace)
        for name in ARTIFACT_NAMES:
            artifacts[name] = publish_file(
                Path(directory) / name, prefix + name, source_commit, verification_dir, aws
            )
        catalog = {
            "schema_version": "contractnli-artifact-catalog-v1",
            "operation": "dataset_prompt_registration",
            "registration_scope": "versioned_s3_manifest",
            "status": "ARTIFACTS_VERIFIED",
            "live_inference": False,
            "source_commit": source_commit, "build_id": build_id,
            "required_build_status": "SUCCEEDED",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "bucket": BUCKET, "prefix": prefix,
            "upstream": manifest["upstream"], "splits": manifest["splits"],
            "prompt": manifest["prompt"], "checks": manifest["checks"],
            "artifacts": artifacts,
        }
        catalog_path = verification_dir / "catalog.json"
        catalog_path.write_bytes(json_bytes(catalog))
        # Publish last. A failed partial build must never be used as a registered catalog.
        # Use a distinct read-back directory to keep the input bytes separate.
        readback = verification_dir / "catalog-readback"
        readback.mkdir()
        receipt = publish_file(
            catalog_path, prefix + "catalog.json", source_commit, readback, aws
        )
    return {
        "operation": catalog["operation"], "live_inference": False,
        "registration_scope": catalog["registration_scope"],
        "build_id": build_id, "source_commit": source_commit,
        "bucket": BUCKET, "catalog": receipt,
        "counts": {
            name: {"documents": value["documents"], "input_rows": value["input_rows"]}
            for name, value in manifest["splits"].items()
        },
        "prompt_sha256": manifest["prompt"]["sha256"],
        "required_build_status": "SUCCEEDED",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    print(json.dumps(publish_catalog(args.input_dir, args.source_commit, os.environ), indent=2))


if __name__ == "__main__":
    main()
