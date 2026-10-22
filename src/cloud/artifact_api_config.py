"""Private AWS configuration preparation and verification; never commit its outputs.

Run prepare in CloudShell. Builds load its private, version/hash-pinned S3 receipt.
No application processing, inference, IAM changes or API deployment occurs here.
"""
import argparse
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from .artifact_api import (
    dataset_metadata, evaluation_metadata, parse_json, prompt_metadata, read_manifest, require,
)

SCHEMA = "contractnli-approved-artifacts-v1"
REGION = "us-east-1"
PROJECT = "legal-llm-cpu-check"
COUNTS = {"train": 423, "validation": 61, "test": 123}
SPECS = {
    "catalog": ("contractnli-artifact-catalog-v1", "dataset_prompt_registration"),
    "evaluation": ("contractnli-evaluation-artifacts-v1", "evaluate_historical_benchmark_predictions"),
}
MAX_BYTES = 64 * 1024


def hex_value(value, length):
    require(isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value),
            "Invalid immutable digest")
    return value


def version_value(value):
    require(isinstance(value, str) and value not in ("", "null")
            and len(value) <= 1024 and not any(c.isspace() for c in value)
            and not (value.startswith("__") and value.endswith("__")), "Invalid object version")
    return value


def canonical_id(value):
    require(isinstance(value, str) and str(uuid.UUID(value)) == value, "Invalid canonical UUID")
    return value


def validate_config(config, bucket=None):
    require(isinstance(config, dict) and config.get("schema_version") == SCHEMA,
            "Unexpected private configuration schema")
    destination = config.get("bucket")
    require(isinstance(destination, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", destination)
            and ".." not in destination, "Invalid artifact bucket")
    require(bucket is None or bucket == destination, "Private configuration bucket differs")
    for name, (schema, operation) in SPECS.items():
        identity = config[name]
        build_id = identity["build_id"]
        require(isinstance(build_id, str) and build_id.startswith(PROJECT + ":"),
                "Unexpected producer project")
        run_id = canonical_id(build_id.split(":", 1)[1])
        prefix = "runs/" + run_id + "/" + name + "/"
        require(identity["prefix"] == prefix and identity["key"] == prefix + name + ".json",
                "Unexpected producer object path")
        require(identity["schema_version"] == schema and identity["operation"] == operation,
                "Unexpected producer operation")
        require(type(identity["bytes"]) is int and 0 < identity["bytes"] <= MAX_BYTES,
                "Manifest size outside limits")
        version_value(identity["version_id"])
        hex_value(identity["sha256"], 64)
        hex_value(identity["source_commit"], 40)
        if name == "catalog":
            require(identity["expected_documents"] == COUNTS
                    and all(type(v) is int for v in identity["expected_documents"].values()),
                    "Unexpected dataset document counts")
        else:
            require(identity["evaluation_id"] == run_id, "Evaluation ID differs")
            hex_value(identity["historical_source_commit"], 40)
    reference = config["reference_test"]
    require(reference["key"] == config["catalog"]["prefix"] + "test.jsonl"
            and type(reference["documents"]) is int and reference["documents"] == COUNTS["test"]
            and type(reference["bytes"]) is int and reference["bytes"] > 0,
            "Unexpected held-out reference")
    version_value(reference["version_id"])
    hex_value(reference["sha256"], 64)
    return config


def render_policy(config, account_id, template):
    validate_config(config)
    require(isinstance(account_id, str) and re.fullmatch(r"[0-9]{12}", account_id),
            "Invalid AWS account ID")
    substitutions = {
        "__AWS_ACCOUNT_ID__": account_id, "__ARTIFACT_BUCKET__": config["bucket"],
        "__CATALOG_MANIFEST_KEY__": config["catalog"]["key"],
        "__CATALOG_MANIFEST_VERSION__": config["catalog"]["version_id"],
        "__EVALUATION_MANIFEST_KEY__": config["evaluation"]["key"],
        "__EVALUATION_MANIFEST_VERSION__": config["evaluation"]["version_id"],
    }
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            for token, replacement in substitutions.items():
                value = value.replace(token, replacement)
            require(re.search(r"__[A-Z_]+__", value) is None, "Unresolved policy token")
        return value
    return replace(deepcopy(template))


def aws_json(*arguments):
    result = subprocess.run(
        ["aws", *arguments, "--region", REGION, "--output", "json", "--no-cli-pager"],
        capture_output=True, text=True, check=False, timeout=90,
    )
    require(result.returncode == 0, "AWS configuration request failed; no deployment performed")
    return parse_json(result.stdout) if result.stdout.strip() else {}


def download_current(bucket, key, version, digest, path):
    version_value(version)
    hex_value(digest, 64)
    metadata = aws_json("s3api", "get-object", "--bucket", bucket, "--key", key, str(path))
    require(metadata.get("VersionId") == version
            and metadata.get("ServerSideEncryption") == "AES256"
            and 0 < path.stat().st_size <= MAX_BYTES
            and metadata.get("ContentLength") == path.stat().st_size,
            "Current object differs from approved version, size or encryption")
    body = path.read_bytes()
    require(hashlib.sha256(body).hexdigest() == digest, "Approved object checksum differs")
    return metadata, body


class SnapshotS3:
    def __init__(self, bucket, snapshots):
        self.bucket, self.snapshots = bucket, snapshots

    def get_object(self, *, Bucket, Key, VersionId):
        require(Bucket == self.bucket and Key in self.snapshots, "Unexpected metadata object")
        metadata, body = self.snapshots[Key]
        require(metadata["VersionId"] == VersionId, "Unexpected metadata version")
        return {**metadata, "Body": io.BytesIO(body)}


def verify_projections(config, snapshots):
    validate_config(config)
    s3 = SnapshotS3(config["bucket"], snapshots)
    catalog = read_manifest(s3, config, "catalog")
    evaluation = read_manifest(s3, config, "evaluation")
    dataset_metadata(catalog, config)
    prompt_metadata(catalog, config)
    evaluation_metadata(evaluation, config)


def load_build_config(output_path):
    bucket = os.environ["OUTPUT_BUCKET"]
    require(os.environ.get("AWS_DEFAULT_REGION") == REGION
            and os.environ.get("CODEBUILD_BUILD_ID", "").startswith(PROJECT + ":"),
            "Run only in the configured AWS CodeBuild project")
    key = os.environ["APPROVED_ARTIFACT_CONFIG_KEY"]
    match = re.fullmatch(r"runs/([0-9a-f-]{36})/config/approved-artifacts\.json", key)
    require(match is not None, "Unexpected private configuration key")
    canonical_id(match.group(1))
    version = os.environ["APPROVED_ARTIFACT_CONFIG_VERSION"]
    digest = os.environ["APPROVED_ARTIFACT_CONFIG_SHA256"]
    with tempfile.TemporaryDirectory(prefix="legal-private-config-") as directory:
        metadata, body = download_current(bucket, key, version, digest, Path(directory) / "config.json")
    require(metadata.get("Metadata", {}).get("sha256") == digest,
            "Private configuration checksum metadata differs")
    config = validate_config(parse_json(body), bucket=bucket)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_private(output_path, body)
    return config


def write_private(path, body):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(body)


def prepare(args):
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output_dir).resolve()
    require(not output.is_relative_to(root), "Write private output outside the repository")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshots, identities, documents = {}, {}, {}
    for name, (schema, operation) in SPECS.items():
        key = getattr(args, name + "_key")
        match = re.fullmatch(r"runs/([0-9a-f-]{36})/" + name + "/" + name + r"\.json", key)
        require(match is not None, "Unexpected manifest key")
        run_id = canonical_id(match.group(1))
        version, digest = getattr(args, name + "_version"), getattr(args, name + "_sha256")
        metadata, body = download_current(
            args.bucket, key, version, digest, output / (name + "-verified.json")
        )
        snapshots[key] = metadata, body
        document = parse_json(body)
        documents[name] = document
        identity = {
            "key": key, "prefix": "runs/" + run_id + "/" + name + "/",
            "version_id": version, "bytes": len(body), "sha256": digest,
            "source_commit": document["source_commit"], "build_id": PROJECT + ":" + run_id,
            "schema_version": schema, "operation": operation,
        }
        if name == "catalog":
            identity["expected_documents"] = dict(COUNTS)
        else:
            identity.update(evaluation_id=run_id,
                            historical_source_commit=document["historical_source_commit"])
        identities[name] = identity

    test = documents["catalog"]["artifacts"]["test.jsonl"]
    config = {
        "schema_version": SCHEMA, "bucket": args.bucket, **identities,
        "reference_test": {
            **{field: test[field] for field in ("key", "version_id", "bytes", "sha256")},
            "documents": COUNTS["test"],
        },
    }
    verify_projections(config, snapshots)
    builds = aws_json("codebuild", "batch-get-builds", "--ids",
                     config["catalog"]["build_id"], config["evaluation"]["build_id"])
    actual = {build["id"]: build for build in builds.get("builds", [])}
    for identity in identities.values():
        build = actual.get(identity["build_id"], {})
        require(build.get("buildStatus") == "SUCCEEDED" and build.get("buildComplete") is True
                and build.get("resolvedSourceVersion") == identity["source_commit"],
                "Producer build is unavailable, unsuccessful or has a different source")

    account = aws_json("sts", "get-caller-identity")["Account"]
    template = parse_json((root / "infra/serverless-demo/artifact-api/execution-policy.template.json")
                          .read_bytes())
    policy = render_policy(config, account, template)
    config_path = output / "approved-artifacts.json"
    body = (json.dumps(config, indent=2) + "\n").encode()
    write_private(config_path, body)
    write_private(output / "execution-policy.json", (json.dumps(policy, indent=2) + "\n").encode())
    digest = hashlib.sha256(body).hexdigest()
    key = "runs/" + str(uuid.uuid4()) + "/config/approved-artifacts.json"
    uploaded = aws_json(
        "s3api", "put-object", "--bucket", args.bucket, "--key", key, "--body", str(config_path),
        "--server-side-encryption", "AES256", "--if-none-match", "*",
        "--metadata", json.dumps({"sha256": digest}),
    )
    version = version_value(uploaded.get("VersionId"))
    download_current(args.bucket, key, version, digest, output / "config-readback.json")
    receipt = {
        "bucket": args.bucket, "key": key, "version_id": version, "sha256": digest,
        "private_directory": str(output),
    }
    write_private(output / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())
    print(json.dumps(receipt, indent=2))
    print("Private configuration verified and uploaded. No IAM, Lambda or API resources changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--output-dir", required=True)
    for name in SPECS:
        for field in ("key", "version", "sha256"):
            parser.add_argument("--" + name + "-" + field, required=True)
    args = parser.parse_args()
    prepare(args)


if __name__ == "__main__":
    main()
