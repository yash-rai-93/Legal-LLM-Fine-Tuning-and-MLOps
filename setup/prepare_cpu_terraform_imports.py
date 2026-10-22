"""Prepare private Terraform import targets from a captured CPU deployment.

No AWS calls, Terraform init/import/plan/apply, tool installation or state edits.
The result is a planning scaffold, not a completed resource configuration.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

TERRAFORM_VERSION = "1.16.6"
AWS_PROVIDER_VERSION = "6.67.0"
FUNCTIONS = {
    "legal-llm-cpu-register": "register",
    "legal-llm-cpu-status-api": "status_api",
    "legal-llm-cpu-artifact-api": "artifact_api",
    "legal-llm-cpu-submission-api": "submission_api",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def label(value):
    result = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    return "cpu_" + result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", required=True)
    args = parser.parse_args()
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    private_root = (Path.home() / "legal-llm-deploy/infrastructure").resolve()
    require(snapshot_dir.is_relative_to(private_root), "Use the private CPU snapshot directory")
    raw = (snapshot_dir / "snapshot.json").read_bytes()
    capture_summary = json.loads((snapshot_dir / "summary.json").read_text())
    snapshot_hash = hashlib.sha256(raw).hexdigest()
    require(snapshot_hash == capture_summary["SnapshotSha256"], "Snapshot checksum mismatch")
    snapshot = json.loads(raw)
    pins = json.loads((snapshot_dir / "package-pins.json").read_text())
    require(snapshot["status"] == "CAPTURED" and not snapshot["errors"]
            and not snapshot["redactions"] and snapshot["region"] == "us-east-1",
            "Require a complete, unredacted CPU snapshot")
    require(pins["status"] == "VERIFIED" and pins["snapshot_sha256"] == snapshot_hash
            and set(pins["functions"]) == set(FUNCTIONS),
            "Require all four verified package pins for this snapshot")
    resources = snapshot["resources"]
    account = snapshot["account"]
    require(re.fullmatch(r"[0-9]{12}", account), "Invalid snapshot account")
    for name in FUNCTIONS:
        package = pins["functions"][name]
        config = resources["functions"][name]["configuration"]
        require(package["zip_verified"] and package["producer_build_verified"]
                and package["code_sha256_base64"] == config["CodeSha256"],
                "Verified package and captured Lambda differ")
        payload = Path(package["local_zip"]).read_bytes()
        require(len(payload) == package["bytes"]
                and hashlib.sha256(payload).hexdigest() == package["sha256"],
                "Recovered ZIP checksum mismatch")
    submission_env = resources["functions"]["legal-llm-cpu-submission-api"]["configuration"]["Environment"]["Variables"]
    require(submission_env["SUBMISSIONS_ENABLED"] == "false", "Snapshot must have disabled submissions")

    targets = []
    addresses = set()
    objects = set()
    external = []

    def add(kind, name, import_id):
        address = kind + "." + name
        require(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name), "Invalid Terraform label")
        require(isinstance(import_id, str) and bool(import_id)
                and address not in addresses and (kind, import_id) not in objects,
                "Duplicate or invalid import target")
        addresses.add(address)
        objects.add((kind, import_id))
        targets.append({"address": address, "type": kind, "id": import_id})

    for name, short_name in FUNCTIONS.items():
        add("aws_lambda_function", short_name, name)
        response = resources["functions"][name]["policy"]
        if response.get("absent"):
            continue
        policy = json.loads(response["Policy"])
        for statement in policy.get("Statement", []):
            sid = statement.get("Sid")
            require(isinstance(sid, str) and bool(sid), "Lambda permission requires an explicit Sid")
            add("aws_lambda_permission", short_name + "_" + label(sid), name + "/" + sid)

    for name, entry in sorted(resources["roles"].items()):
        role_name = entry["role"]["Role"]["RoleName"]
        require(role_name == name and "legal-llm-cpu" in name,
                "Review ownership of a referenced role outside the CPU naming scope")
        role_label = label(name)
        add("aws_iam_role", role_label, name)
        for policy_name in sorted(entry["inline_policies"]):
            add("aws_iam_role_policy", role_label + "_" + label(policy_name),
                name + ":" + policy_name)
        for attachment in entry["attached_policies"]:
            arn = attachment["PolicyArn"]
            suffix = hashlib.sha256(arn.encode()).hexdigest()[:12]
            add("aws_iam_role_policy_attachment", role_label + "_" + suffix, name + "/" + arn)
            external.append({"kind": "managed_policy_definition", "arn": arn})

    project = resources["codebuild"]["projects"][0]
    require(project["name"] == "legal-llm-cpu-check", "Unexpected CodeBuild project")
    add("aws_codebuild_project", "cpu", project["name"])
    if project.get("source", {}).get("auth"):
        external.append({"kind": "codebuild_source_authorization",
                         "configuration": project["source"]["auth"]})

    workflow = resources["workflow"]["current"]
    add("aws_sfn_state_machine", "cpu", workflow["stateMachineArn"])
    external.append({"kind": "immutable_workflow_version",
                     "arn": submission_env["WORKFLOW_VERSION_ARN"]})

    api = resources["http_api"]
    api_id = api["configuration"]["ApiId"]
    require(api_id == submission_env["HTTP_API_ID"]
            and api["configuration"]["ProtocolType"] == "HTTP"
            and not api["authorizers"], "Review unexpected API configuration")
    add("aws_apigatewayv2_api", "cpu", api_id)
    for route in sorted(api["routes"], key=lambda item: item["RouteKey"]):
        require(route.get("AuthorizationType") == "AWS_IAM"
                and not route.get("ApiGatewayManaged", False),
                "Review an unmanaged or differently authorized API route")
        add("aws_apigatewayv2_route", label(route["RouteKey"]), api_id + "/" + route["RouteId"])
    for integration in sorted(api["integrations"], key=lambda item: item["IntegrationId"]):
        require(not integration.get("ApiGatewayManaged", False), "Managed integration cannot be imported")
        add("aws_apigatewayv2_integration", label(integration["IntegrationId"]),
            api_id + "/" + integration["IntegrationId"])
    for stage in sorted(api["stages"], key=lambda item: item["StageName"]):
        require(not stage.get("ApiGatewayManaged", False), "Managed stage cannot be imported")
        add("aws_apigatewayv2_stage", label(stage["StageName"]), api_id + "/" + stage["StageName"])

    for name in sorted(resources["tables"]):
        add("aws_dynamodb_table", label(name), name)
    bucket = resources["bucket"]
    add("aws_s3_bucket", "artifacts", bucket["name"])
    bucket_parts = {
        "get_bucket_versioning": ("aws_s3_bucket_versioning", "Status"),
        "get_bucket_encryption": ("aws_s3_bucket_server_side_encryption_configuration", "ServerSideEncryptionConfiguration"),
        "get_public_access_block": ("aws_s3_bucket_public_access_block", "PublicAccessBlockConfiguration"),
        "get_bucket_ownership_controls": ("aws_s3_bucket_ownership_controls", "OwnershipControls"),
        "get_bucket_policy": ("aws_s3_bucket_policy", "Policy"),
        "get_bucket_lifecycle_configuration": ("aws_s3_bucket_lifecycle_configuration", "Rules"),
    }
    for operation, (kind, key) in bucket_parts.items():
        found = bucket[operation]
        if found.get("absent"):
            continue
        if found.get(key):
            add(kind, "artifacts", bucket["name"])

    for group in sorted(resources["logs"], key=lambda item: item["logGroupName"]):
        add("aws_cloudwatch_log_group", label(group["logGroupName"]), group["logGroupName"])
    require(not resources["alarms"].get("CompositeAlarms"), "Review unexpected composite alarms")
    for alarm in sorted(resources["alarms"]["MetricAlarms"], key=lambda item: item["AlarmName"]):
        add("aws_cloudwatch_metric_alarm", label(alarm["AlarmName"]), alarm["AlarmName"])
    topic = resources["alerts"]["topic"]["Attributes"]["TopicArn"]
    add("aws_sns_topic", "alerts", topic)
    for subscription in resources["alerts"]["subscriptions"]:
        external.append({"kind": "operator_confirmed_subscription",
                         "arn": subscription["SubscriptionArn"],
                         "protocol": subscription["Protocol"]})

    os.umask(0o077)
    workspace = snapshot_dir / "terraform-planning" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    workspace.mkdir(parents=True, mode=0o700)

    def write(name, text):
        path = workspace / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def write_json(name, body):
        write(name, json.dumps(body, indent=2) + "\n")

    root = {
        "terraform": {
            "required_version": "= " + TERRAFORM_VERSION,
            "required_providers": {
                "aws": {"source": "hashicorp/aws", "version": "= " + AWS_PROVIDER_VERSION},
            },
            "backend": {"local": {"path": str(workspace / "terraform.tfstate")}},
        },
        "provider": {"aws": {"region": "us-east-1", "allowed_account_ids": [account]}},
    }
    write_json("versions.tf.json", root)
    hcl = "# Private existing-resource import targets. Planning scaffold only.\n\n"
    for item in targets:
        hcl += "import {\n  to = " + item["address"] + "\n  id = " + json.dumps(item["id"]) + "\n}\n\n"
    write("imports.tf", hcl)
    write_json("import-targets.json", {"snapshot_sha256": snapshot_hash, "targets": targets})
    write_json("deployed-package-pins.json", pins)
    write_json("external-dependencies.json", external)
    write("README.txt",
          "PRIVATE PLANNING SCAFFOLD. No imports or AWS changes have occurred.\n"
          "Resource configurations are not generated yet. Do not apply this scaffold.\n"
          "Keep this separate from the original Terraform/ stack and all existing states.\n"
          "Review prior state ownership and the external dependencies before adoption.\n"
          "Lambda S3 source locations are not refreshable during import; retain the verified pins.\n"
          "Provider-generated IAM/S3 legacy fields must not duplicate separate policy/config resources.\n"
          "Only a reviewed plan with imports and zero resource changes can proceed to adoption.\n")
    executable = shutil.which("terraform")
    installed_version = None
    if executable:
        try:
            result = subprocess.run([executable, "version", "-json"], capture_output=True,
                                    text=True, check=False, timeout=15)
            if result.returncode == 0:
                installed_version = json.loads(result.stdout).get("terraform_version")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    output = {
        "Preparation": "READY", "ImportTargets": len(targets),
        "ResourceTypes": dict(sorted(Counter(item["type"] for item in targets).items())),
        "VerifiedLambdaPackages": len(pins["functions"]),
        "ExternalDependencies": len(external),
        "WorkspaceDirectory": str(workspace),
        "TerraformInstalled": bool(executable), "InstalledTerraformVersion": installed_version,
        "RequiredTerraformVersion": TERRAFORM_VERSION, "AWSProviderVersion": AWS_PROVIDER_VERSION,
        "Architecture": platform.machine(),
        "HomeFreeMiB": shutil.disk_usage(Path.home()).free // (1024 * 1024),
        "TempFreeMiB": shutil.disk_usage("/tmp").free // (1024 * 1024),
        "ResourceConfigurationGenerated": False, "TerraformPlanStarted": False,
        "AWSChangesMade": False, "TerraformStateChanged": False,
    }
    write_json("preparation-summary.json", output)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"Preparation": "STOPPED", "Error": type(exc).__name__,
                          "Reason": str(exc) if type(exc) is ValueError else "Inspect private input files",
                          "AWSChangesMade": False, "TerraformStateChanged": False}))
        raise SystemExit(1)
