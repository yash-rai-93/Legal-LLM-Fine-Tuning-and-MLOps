"""Verify CPU build provenance and versioned S3 outputs, then register one run.

No model imports, resource creation, public API, or prediction generation.
The Lambda ZIP contains this module only; the managed runtime supplies boto3.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import uuid

SCHEMA_VERSION = "contractnli-cpu-v1"
REGISTRY_VERSION = "contractnli-run-v1"
REPOSITORY = "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DATA_BYTES = 128 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _commit(value):
    _require(isinstance(value, str) and COMMIT.fullmatch(value), "Invalid source commit")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate manifest JSON key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Non-finite manifest JSON number")


def validate_manifest(manifest, source_commit):
    _require(isinstance(manifest, dict), "Manifest must be a JSON object")
    _require(manifest.get("schema_version") == SCHEMA_VERSION, "Unknown manifest schema")
    _require(manifest.get("operation") == "data_preparation", "Unexpected operation")
    _require(manifest.get("live_inference") is False, "Expected CPU-only data preparation")
    _require(manifest.get("source_commit") == source_commit, "Manifest source commit mismatch")
    _require(
        manifest.get("dataset_source") == "https://stanfordnlp.github.io/contract-nli/"
        and manifest.get("dataset_license") == "CC BY 4.0",
        "Dataset provenance mismatch",
    )
    try:
        timestamp = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
        _require(timestamp.utcoffset() is not None, "Manifest timestamp needs a timezone")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Invalid manifest timestamp") from exc
    splits = manifest.get("splits")
    _require(
        isinstance(splits, dict) and set(splits) == {"train", "validation"},
        "Expected exactly train and validation splits",
    )
    counts = {}
    for name, split in splits.items():
        _require(isinstance(split, dict), "Invalid split metadata")
        documents, rows = split.get("documents"), split.get("input_rows")
        _require(
            type(documents) is int and documents > 0
            and type(rows) is int and rows == 17 * documents,
            "Invalid document or hypothesis-row count",
        )
        for field in ("input_sha256", "semantic_sha256", "output_sha256"):
            value = split.get(field)
            _require(isinstance(value, str) and SHA256.fullmatch(value), "Invalid manifest hash")
        counts[name] = {"documents": documents, "input_rows": rows}
    return counts


def read_versioned_object(s3, bucket, key, *, max_bytes, capture=False):
    metadata = s3.head_object(Bucket=bucket, Key=key)
    version = metadata.get("VersionId")
    size = metadata.get("ContentLength")
    _require(
        isinstance(version, str) and version not in ("", "null"),
        "A non-null S3 object version is required",
    )
    _require(metadata.get("ServerSideEncryption") == "AES256", "Expected SSE-S3 encryption")
    _require(type(size) is int and 0 < size <= max_bytes, "S3 object size is outside limits")
    response = s3.get_object(Bucket=bucket, Key=key, VersionId=version)
    body = response["Body"]
    digest, length, chunks = hashlib.sha256(), 0, []
    try:
        _require(response.get("VersionId") == version, "S3 returned a different object version")
        while True:
            chunk = body.read(64 * 1024)
            if not chunk:
                break
            length += len(chunk)
            _require(length <= size, "S3 body exceeds its declared size")
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
    finally:
        body.close()
    _require(length == size, "S3 object body is truncated")
    result = {
        "key": key, "version_id": version, "sha256": digest.hexdigest(),
        "bytes": length, "encryption": "AES256",
    }
    return result, b"".join(chunks) if capture else None


def register_run(event, *, s3, codebuild, table, bucket, project, processor_commit, registry_commit):
    """Clients are injected for cloud-run unit tests; no AWS client is created here."""
    _commit(processor_commit)
    _commit(registry_commit)
    _require(isinstance(event, dict), "Workflow event must be an object")
    _require(event.get("operation") == "data_preparation", "Unexpected workflow operation")
    _require(event.get("live_inference") is False, "Expected a CPU workflow")
    _require(event.get("bucket") == bucket, "Unexpected artifact bucket")
    _require(event.get("source_commit") == processor_commit, "Unapproved processor commit")
    _require(event.get("build_status") == "SUCCEEDED", "Build did not succeed")
    build_id = event.get("build_id")
    _require(isinstance(build_id, str) and build_id.startswith(project + ":"), "Unexpected build project")
    run_id = build_id[len(project) + 1:]
    try:
        _require(str(uuid.UUID(run_id)) == run_id, "Invalid run ID")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid run ID") from exc
    prefix = "runs/" + run_id + "/"
    _require(event.get("prefix") == prefix, "Unexpected artifact prefix")

    result = codebuild.batch_get_builds(ids=[build_id])
    builds = result.get("builds", [])
    _require(len(builds) == 1 and not result.get("buildsNotFound"), "Build was not found")
    build = builds[0]
    _require(
        build.get("id") == build_id and build.get("buildStatus") == "SUCCEEDED"
        and build.get("buildComplete") is True,
        "CodeBuild has not confirmed successful completion",
    )
    _require(build.get("resolvedSourceVersion") == processor_commit, "CodeBuild source mismatch")
    source = build.get("source", {})
    location = source.get("location", "")
    _require(
        source.get("type") == "GITHUB"
        and source.get("buildspec") == "buildspecs/cpu-publish.yml"
        and isinstance(location, str)
        and location.rstrip("/").removesuffix(".git") == REPOSITORY,
        "Unexpected CodeBuild repository or buildspec",
    )
    environment = build.get("environment", {}).get("environmentVariables", [])
    destinations = [entry.get("value") for entry in environment if entry.get("name") == "OUTPUT_BUCKET"]
    _require(destinations == [bucket], "CodeBuild output bucket mismatch")

    manifest_object, payload = read_versioned_object(
        s3, bucket, prefix + "manifest.json", max_bytes=MAX_MANIFEST_BYTES, capture=True
    )
    try:
        manifest = json.loads(
            payload, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
        )
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid manifest JSON") from exc
    counts = validate_manifest(manifest, processor_commit)
    artifacts = {"manifest.json": manifest_object}
    for split in ("train", "validation"):
        artifact, _ = read_versioned_object(
            s3, bucket, prefix + split + ".jsonl", max_bytes=MAX_DATA_BYTES
        )
        _require(
            artifact["sha256"] == manifest["splits"][split]["output_sha256"],
            "Output checksum mismatch for " + split,
        )
        artifacts[split + ".jsonl"] = artifact

    identity = {
        "registry_version": REGISTRY_VERSION, "build_id": build_id,
        "source_commit": processor_commit, "bucket": bucket, "prefix": prefix,
        "artifacts": artifacts, "counts": counts,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    item = {
        **identity, "run_id": run_id, "status": "COMPLETED",
        "operation": "data_preparation", "live_inference": False,
        "fingerprint": fingerprint, "registry_commit": registry_commit,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "manifest_created_at": manifest["created_at"],
        "dataset_source": manifest["dataset_source"],
        "dataset_license": manifest["dataset_license"],
        "splits": manifest["splits"],
    }
    created = True
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(run_id)")
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(Key={"run_id": run_id}, ConsistentRead=True).get("Item", {})
        _require(
            existing.get("fingerprint") == fingerprint and existing.get("status") == "COMPLETED",
            "Existing run conflicts with these artifact versions",
        )
        item, created = existing, False
    return {
        "run_id": run_id, "status": "COMPLETED", "created": created,
        "operation": "data_preparation", "live_inference": False,
        "build_id": build_id, "source_commit": processor_commit,
        "registry_commit": item["registry_commit"],
        "bucket": bucket, "prefix": prefix, "counts": counts,
        "manifest_version_id": artifacts["manifest.json"]["version_id"],
        "registered_at": item["registered_at"],
    }


def lambda_handler(event, _context):
    # boto3 is supplied by the managed Lambda runtime. No vendored SDK packages
    # are partially overridden; runtime SDK updates remain a deployment boundary.
    import boto3
    from botocore.config import Config

    sdk_config = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2})
    dynamodb = boto3.resource("dynamodb", config=sdk_config)
    return register_run(
        event,
        s3=boto3.client("s3", config=sdk_config),
        codebuild=boto3.client("codebuild", config=sdk_config),
        table=dynamodb.Table(os.environ["RUN_TABLE"]),
        bucket=os.environ["OUTPUT_BUCKET"],
        project=os.environ["CODEBUILD_PROJECT"],
        processor_commit=os.environ["PROCESSOR_SOURCE_COMMIT"],
        registry_commit=os.environ["REGISTRY_SOURCE_COMMIT"],
    )
