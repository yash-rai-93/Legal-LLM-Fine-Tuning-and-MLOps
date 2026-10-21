"""Package/upload the thin registry Lambda from AWS CodeBuild only.

Uses the AWS CLI and Python standard library. Creates no AWS resources.
All uploads are scoped to the existing build role's runs/ prefix.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile


def aws_json(*arguments):
    result = subprocess.run(
        ["aws", *arguments, "--region", "us-east-1", "--output", "json", "--no-cli-pager"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "AWS CLI request failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def publish(path, *, bucket, key, source_commit, output_dir):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    uploaded = aws_json(
        "s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path),
        "--server-side-encryption", "AES256",
        "--metadata", json.dumps({"source-commit": source_commit, "sha256": digest}),
    )
    version = uploaded.get("VersionId")
    if not version or version == "null":
        raise ValueError("S3 did not assign a non-null object version")
    # The existing build role has GetObject, not GetObjectVersion. Read the
    # current object, require the returned version to match, then compare bytes.
    downloaded_path = output_dir / ("verified-" + path.name)
    downloaded = aws_json(
        "s3api", "get-object", "--bucket", bucket, "--key", key, str(downloaded_path)
    )
    if (
        downloaded.get("VersionId") != version
        or downloaded.get("ServerSideEncryption") != "AES256"
        or hashlib.sha256(downloaded_path.read_bytes()).hexdigest() != digest
    ):
        raise ValueError("Uploaded package metadata or bytes do not match")
    return {
        "bucket": bucket, "key": key, "version_id": version,
        "sha256": digest, "bytes": path.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        raise ValueError("An immutable package source commit is required")
    build_id = os.environ["CODEBUILD_BUILD_ID"]
    project, run_id = build_id.split(":", 1)
    if project != "legal-llm-cpu-check" or not re.fullmatch(r"[0-9a-f-]{36}", run_id):
        raise ValueError("Unexpected package build identity")
    bucket = os.environ["OUTPUT_BUCKET"]
    if bucket != "yash-legal-llm-cpu-20261022" or os.environ["AWS_DEFAULT_REGION"] != "us-east-1":
        raise ValueError("Unexpected package destination or region")
    location = aws_json("s3api", "get-bucket-location", "--bucket", bucket)
    if (location.get("LocationConstraint") or "us-east-1") != "us-east-1":
        raise ValueError("Package bucket must be in us-east-1")
    if aws_json("s3api", "get-bucket-versioning", "--bucket", bucket).get("Status") != "Enabled":
        raise ValueError("Enable versioning before publishing packages")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / "src/cloud/registry.py"
    package = output_dir / "cpu-register.zip"
    entry = zipfile.ZipInfo("registry.py", date_time=(2020, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.external_attr = 0o644 << 16
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(entry, source.read_bytes())
    prefix = "runs/" + run_id + "/packages/"
    metadata = publish(
        package, bucket=bucket, key=prefix + package.name,
        source_commit=args.source_commit, output_dir=output_dir,
    )
    manifest = {
        "schema_version": "contractnli-registry-package-v1",
        "operation": "registry_package", "live_inference": False,
        "package_source_commit": args.source_commit, "build_id": build_id,
        "runtime": "python3.12", "handler": "registry.lambda_handler", **metadata,
    }
    manifest_path = output_dir / "package.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    publish(
        manifest_path, bucket=bucket, key=prefix + "package.json",
        source_commit=args.source_commit, output_dir=output_dir,
    )
    print(json.dumps(manifest, indent=2))
    print("Published and byte-verified registry ZIP and package manifest.")
    print("No Lambda, DynamoDB table or workflow update has been deployed.")


if __name__ == "__main__":
    main()
