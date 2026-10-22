"""Recover immutable thin-Lambda package pins from S3 using deployed hashes.

CloudShell administration only: read S3/package metadata and producer builds,
verify bounded ZIP bytes, and save private files. No rebuild or deployment.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import zipfile

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session

REGION = "us-east-1"
PROJECT = "legal-llm-cpu-check"
REPOSITORY = "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps"
PROFILES = {
    "legal-llm-cpu-register": ("registry", "registry.py", "cpu-register.zip"),
    "legal-llm-cpu-status-api": ("status-api", "status_api.py", "cpu-status-api.zip"),
    "legal-llm-cpu-artifact-api": ("artifact-api", "artifact_api.py", "cpu-artifact-api.zip"),
    "legal-llm-cpu-submission-api": ("submission-api", "submission_api.py", "cpu-submission-api.zip"),
}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MANIFEST_KEY = re.compile(r"runs/(" + UUID + r")/packages/package\.json")
MAX_MANIFEST = 64 * 1024
MAX_ZIP = 256 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", required=True)
    args = parser.parse_args()
    directory = Path(args.snapshot_dir).expanduser().resolve()
    private_root = (Path.home() / "legal-llm-deploy/infrastructure").resolve()
    require(directory.is_relative_to(private_root), "Use a private infrastructure snapshot directory")
    raw_snapshot = (directory / "snapshot.json").read_bytes()
    snapshot = json.loads(raw_snapshot)
    summary = json.loads((directory / "summary.json").read_text())
    require(hashlib.sha256(raw_snapshot).hexdigest() == summary["SnapshotSha256"],
            "Snapshot hash mismatch")
    require(snapshot["schema_version"] == "contractnli-cpu-infrastructure-snapshot-v1"
            and snapshot["region"] == REGION and snapshot["status"] == "CAPTURED",
            "Require a completed CPU infrastructure snapshot")

    session = Session()
    config = Config(connect_timeout=5, read_timeout=10,
                    retries={"total_max_attempts": 2})
    sts = session.create_client("sts", region_name=REGION, config=config)
    account = sts.get_caller_identity()["Account"]
    require(account == snapshot["account"], "Snapshot belongs to a different AWS account")
    lambdas = session.create_client("lambda", region_name=REGION, config=config)
    s3 = session.create_client("s3", region_name=REGION, config=config)
    codebuild = session.create_client("codebuild", region_name=REGION, config=config)
    functions = snapshot["resources"]["functions"]
    bucket = snapshot["resources"]["bucket"]["name"]

    def live_configuration(name):
        current = lambdas.get_function_configuration(FunctionName=name)
        captured = functions[name]["configuration"]
        require(current["FunctionArn"] == f"arn:aws:lambda:{REGION}:{account}:function:{name}"
                and current["State"] == "Active" and current["LastUpdateStatus"] == "Successful"
                and current["CodeSha256"] == captured["CodeSha256"]
                and current["Runtime"] == captured["Runtime"]
                and current["Handler"] == captured["Handler"],
                "Deployed Lambda changed or is unavailable")
        if name == "legal-llm-cpu-submission-api":
            require(current.get("Environment", {}).get("Variables", {}).get("SUBMISSIONS_ENABLED") == "false",
                    "Submissions must remain disabled")
        return current

    targets = {}
    for name in PROFILES:
        targets[name] = live_configuration(name)
    wanted_hashes = {base64.b64decode(item["CodeSha256"], validate=True).hex()
                     for item in targets.values()}

    os.umask(0o077)
    output = directory / "package-recovery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, mode=0o700)
    report = {
        "schema_version": "contractnli-cpu-package-pins-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_sha256": summary["SnapshotSha256"],
        "status": "SCANNING", "functions": {}, "rejected": [],
        "aws_changes_made": False, "terraform_state_changed": False,
    }

    def write_json(path, body):
        path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def checkpoint():
        write_json(output / "recovery.json", report)

    def object_bytes(key, version=None, maximum=MAX_MANIFEST):
        parameters = {"Bucket": bucket, "Key": key, "ExpectedBucketOwner": account}
        if version is not None:
            parameters["VersionId"] = version
        response = s3.get_object(**parameters)
        stream = response["Body"]
        try:
            require(0 < response["ContentLength"] <= maximum, "Object size outside bounds")
            payload = stream.read(maximum + 1)
        finally:
            stream.close()
        require(len(payload) == response["ContentLength"], "Object byte length mismatch")
        require(response.get("ServerSideEncryption") == "AES256", "Expected SSE-S3 encryption")
        actual_version = response.get("VersionId")
        require(isinstance(actual_version, str) and actual_version not in ("", "null"),
                "Require a non-null S3 object version")
        require(version is None or actual_version == version, "S3 version mismatch")
        digest = hashlib.sha256(payload).hexdigest()
        require(response.get("Metadata", {}).get("sha256") == digest, "S3 metadata hash mismatch")
        return payload, response, digest

    checkpoint()
    candidates = []
    scanned_objects = 0
    parameters = {"Bucket": bucket, "Prefix": "runs/", "MaxKeys": 1000,
                  "ExpectedBucketOwner": account}
    while True:
        page = s3.list_objects_v2(**parameters)
        for item in page.get("Contents", []):
            scanned_objects += 1
            require(scanned_objects <= 5000, "Object listing bound reached; review inventory scope")
            if MANIFEST_KEY.fullmatch(item["Key"]):
                require(len(candidates) < 100, "Manifest listing bound reached; review inventory scope")
                candidates.append(item["Key"])
        if not page.get("IsTruncated"):
            break
        parameters["ContinuationToken"] = page["NextContinuationToken"]
    report["manifest_candidates"] = len(candidates)
    checkpoint()
    print(json.dumps({"PackageManifestCandidates": len(candidates)}), flush=True)

    builds = {}
    for key in sorted(candidates):
        if len(report["functions"]) == len(PROFILES):
            break
        try:
            manifest_raw, manifest_response, manifest_hash = object_bytes(key)
            manifest = json.loads(manifest_raw)
            if not isinstance(manifest, dict) or manifest.get("sha256") not in wanted_hashes:
                continue
            run_id = MANIFEST_KEY.fullmatch(key).group(1)
            for name, (component, module, filename) in PROFILES.items():
                target = targets[name]
                if name in report["functions"]:
                    continue
                target_hex = base64.b64decode(target["CodeSha256"], validate=True).hex()
                if manifest.get("sha256") != target_hex:
                    continue
                require(manifest.get("schema_version") == f"contractnli-{component}-package-v1"
                        and manifest.get("operation") == component.replace("-", "_") + "_package"
                        and manifest.get("component", component) == component
                        and manifest.get("live_inference") is False,
                        "Package manifest identity mismatch")
                require(manifest.get("bucket") == bucket
                        and manifest.get("key") == f"runs/{run_id}/packages/{filename}"
                        and manifest.get("build_id") == PROJECT + ":" + run_id
                        and manifest.get("runtime") == target["Runtime"]
                        and manifest.get("handler") == target["Handler"],
                        "Package deployment metadata mismatch")
                commit = manifest.get("package_source_commit", "")
                require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
                        "Require immutable package source commit")
                require(isinstance(manifest.get("version_id"), str)
                        and manifest["version_id"] not in ("", "null")
                        and type(manifest.get("bytes")) is int
                        and 0 < manifest["bytes"] <= MAX_ZIP,
                        "Package version/size invalid")
                build_id = manifest["build_id"]
                if build_id not in builds:
                    found = codebuild.batch_get_builds(ids=[build_id])
                    require(not found.get("buildsNotFound") and len(found.get("builds", [])) == 1,
                            "Package producer build unavailable")
                    builds[build_id] = found["builds"][0]
                build = builds[build_id]
                location = build.get("source", {}).get("location", "").rstrip("/").removesuffix(".git")
                destinations = [item.get("value") for item in build.get("environment", {}).get("environmentVariables", [])
                                if item.get("name") == "OUTPUT_BUCKET"]
                require(build.get("id") == build_id and build.get("projectName") == PROJECT
                        and build.get("buildStatus") == "SUCCEEDED" and build.get("buildComplete") is True
                        and build.get("resolvedSourceVersion") == commit
                        and build.get("source", {}).get("type") == "GITHUB" and location == REPOSITORY
                        and build.get("source", {}).get("buildspec") == f"buildspecs/{component}-check.yml"
                        and destinations == [bucket],
                        "Package producer provenance mismatch")

                payload, package_response, digest = object_bytes(
                    manifest["key"], manifest["version_id"], maximum=MAX_ZIP)
                require(digest == manifest["sha256"] and len(payload) == manifest["bytes"],
                        "Package bytes differ from manifest or Lambda")
                require(package_response.get("Metadata", {}).get("source-commit") == commit,
                        "Package source metadata mismatch")
                expected_entries = {module}
                if component == "artifact-api":
                    expected_entries.add("approved_artifacts.json")
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    entries = archive.infolist()
                    require(len(entries) == len(expected_entries)
                            and {entry.filename for entry in entries} == expected_entries
                            and all(0 < entry.file_size <= MAX_ZIP and not entry.is_dir()
                                    for entry in entries), "Unexpected package ZIP entries")
                    require(archive.testzip() is None, "Package ZIP CRC failure")
                    if component == "artifact-api":
                        require(isinstance(json.loads(archive.read("approved_artifacts.json")), dict),
                                "Artifact approval configuration is not a JSON object")

                zip_path = output / filename
                zip_path.write_bytes(payload)
                zip_path.chmod(0o600)
                manifest_path = output / ("package-" + component + ".json")
                manifest_path.write_bytes(manifest_raw)
                manifest_path.chmod(0o600)
                report["functions"][name] = {
                    "component": component, "runtime": target["Runtime"], "handler": target["Handler"],
                    "code_sha256_base64": target["CodeSha256"],
                    "bucket": bucket, "key": manifest["key"], "version_id": manifest["version_id"],
                    "sha256": digest, "bytes": len(payload), "source_commit": commit,
                    "build_id": build_id, "local_zip": str(zip_path),
                    "manifest_reference": {"bucket": bucket, "key": key,
                                           "version_id": manifest_response["VersionId"],
                                           "sha256": manifest_hash},
                    "zip_verified": True, "producer_build_verified": True,
                }
                checkpoint()
                print(json.dumps({"PackageVerified": component}), flush=True)
        except (ClientError, BotoCoreError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError") if isinstance(exc, ClientError) else type(exc).__name__
            report["rejected"].append({"manifest_key": key, "code": code,
                                       "reason": str(exc) if type(exc) is ValueError else code})
            checkpoint()

    for name in PROFILES:
        live_configuration(name)
    complete = len(report["functions"]) == len(PROFILES)
    report["status"] = "VERIFIED" if complete else "INCOMPLETE"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    if complete:
        write_json(directory / "package-pins.json", report)
    result = {
        "PackageRecovery": report["status"],
        "FunctionsPinned": len(report["functions"]), "ExpectedFunctions": len(PROFILES),
        "UnmatchedFunctions": sorted(set(PROFILES) - set(report["functions"])),
        "SelectedZIPsVerified": sum(item["zip_verified"] for item in report["functions"].values()),
        "ProducerBuildsVerified": sum(item["producer_build_verified"] for item in report["functions"].values()),
        "RejectedCandidates": len(report["rejected"]),
        "RejectedCandidateReasons": sorted({item["reason"] for item in report["rejected"]}),
        "SubmissionsEnabled": False,
        "PrivateDirectory": str(output),
        "PinsFile": str(directory / "package-pins.json") if complete else None,
        "AWSChangesMade": False, "TerraformStateChanged": False,
    }
    write_json(output / "summary.json", result)
    print(json.dumps(result, indent=2))
    return 0 if complete else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ClientError, BotoCoreError, ValueError, KeyError) as exc:
        code = exc.response.get("Error", {}).get("Code", "ClientError") if isinstance(exc, ClientError) else type(exc).__name__
        print(json.dumps({"PackageRecovery": "STOPPED", "Error": code,
                          "Reason": str(exc) if type(exc) is ValueError else code,
                          "AWSChangesMade": False, "TerraformStateChanged": False}))
        raise SystemExit(1)
