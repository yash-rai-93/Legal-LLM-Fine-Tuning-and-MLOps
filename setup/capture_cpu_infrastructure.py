"""Read-only CloudShell snapshot of the existing CPU deployment.

Writes a private inventory for later infrastructure adoption. It does not run
Terraform, change AWS resources, fetch Lambda download URLs or copy table data.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import Session

REGION = "us-east-1"
PROJECT = "legal-llm-cpu-check"
FUNCTIONS = (
    "legal-llm-cpu-register", "legal-llm-cpu-status-api",
    "legal-llm-cpu-artifact-api", "legal-llm-cpu-submission-api",
)
TABLES = ("legal-llm-cpu-runs", "legal-llm-cpu-submissions")
ALARMS = (
    "legal-llm-cpu-workflow-failures", "legal-llm-cpu-lambda-errors",
    "legal-llm-cpu-api-server-errors",
)
ENVIRONMENT_KEYS = {
    "HTTP_API_ID", "RUN_TABLE", "OUTPUT_BUCKET", "CODEBUILD_PROJECT",
    "PROCESSOR_SOURCE_COMMIT", "REGISTRY_SOURCE_COMMIT",
    "SUBMISSION_TABLE", "WORKFLOW_VERSION_ARN", "SUBMISSIONS_ENABLED",
}


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()
                if key != "ResponseMetadata"}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+", args.api_id):
        raise SystemExit("Use the existing CPU HTTP API ID.")

    session = Session()
    config = Config(connect_timeout=5, read_timeout=10,
                    retries={"total_max_attempts": 2})
    clients = {}
    errors = []
    redactions = []

    def read(service, operation, absent=(), **parameters):
        if not operation.startswith(("get_", "list_", "describe_", "batch_get_")):
            raise ValueError("Inventory permits read operations only")
        try:
            if service not in clients:
                clients[service] = session.create_client(
                    service, region_name=REGION, config=config)
            return clean(getattr(clients[service], operation)(**parameters))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            if code in absent:
                return {"absent": True, "reason": code}
            errors.append({"service": service, "operation": operation, "code": code})
            return {"error": code}
        except BotoCoreError as exc:
            code = type(exc).__name__
            errors.append({"service": service, "operation": operation, "code": code})
            return {"error": code}

    def collect(service, operation, key, **parameters):
        items = []
        tokens = set()
        while True:
            page = read(service, operation, **parameters)
            items.extend(page.get(key, []))
            if page.get("IsTruncated") is False:
                break
            token_name = next((name for name in ("NextToken", "nextToken", "Marker")
                               if page.get(name)), None)
            if token_name is None:
                break
            token = page[token_name]
            if token in tokens:
                raise ValueError("Pagination repeated a token")
            tokens.add(token)
            parameters[token_name] = token
        return items

    identity = read("sts", "get_caller_identity")
    account = identity.get("Account", "")
    if not re.fullmatch(r"[0-9]{12}", account):
        raise SystemExit("Could not establish the signed-in AWS account.")
    workflow = f"arn:aws:states:{REGION}:{account}:stateMachine:legal-llm-cpu-prepare"
    topic = f"arn:aws:sns:{REGION}:{account}:legal-llm-cpu-alerts"
    base = Path.home() / "legal-llm-deploy"
    os.umask(0o077)
    directory = base / "infrastructure" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True, mode=0o700)
    snapshot = {
        "schema_version": "contractnli-cpu-infrastructure-snapshot-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "region": REGION, "account": account,
        "resources": {}, "errors": errors, "redactions": redactions,
        "aws_changes_made": False, "terraform_state_changed": False,
    }

    def save():
        path = directory / "snapshot.json"
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        path.chmod(0o600)

    def checkpoint(stage):
        save()
        print(json.dumps({"CapturedStage": stage}), flush=True)

    resources = snapshot["resources"]
    functions = resources["functions"] = {}
    roles = set()
    for name in FUNCTIONS:
        found = read("lambda", "get_function_configuration", FunctionName=name)
        variables = found.get("Environment", {}).get("Variables", {})
        for key in list(variables):
            if key not in ENVIRONMENT_KEYS:
                variables[key] = "[REDACTED]"
                redactions.append({"scope": name, "variable": key})
        functions[name] = {
            "configuration": found,
            "concurrency": read("lambda", "get_function_concurrency", FunctionName=name),
            "policy": read("lambda", "get_policy", FunctionName=name,
                           absent=("ResourceNotFoundException",)),
            "tags": read("lambda", "list_tags", Resource=found["FunctionArn"])
                    if found.get("FunctionArn") else {},
        }
        if found.get("Role"):
            roles.add(found["Role"])
    checkpoint("functions")

    submission = functions["legal-llm-cpu-submission-api"]["configuration"]
    env = submission.get("Environment", {}).get("Variables", {})
    version = env.get("WORKFLOW_VERSION_ARN", "")
    if (any(entry["configuration"].get("State") != "Active"
            or entry["configuration"].get("LastUpdateStatus") != "Successful"
            for entry in functions.values())
            or env.get("SUBMISSIONS_ENABLED") != "false"
            or env.get("HTTP_API_ID") != args.api_id
            or not re.fullmatch(re.escape(workflow) + r":[1-9][0-9]*", version)):
        snapshot["guard_failure"] = "Require disabled submissions, the configured API and numeric CPU workflow version"
        save()
        raise SystemExit("Capture stopped: submission configuration guard failed; private partial snapshot saved.")

    project_result = read("codebuild", "batch_get_projects", names=[PROJECT])
    projects = project_result.get("projects", [])
    project = projects[0] if len(projects) == 1 else {}
    resources["codebuild"] = project_result
    if project.get("webhook", {}).get("secret"):
        project["webhook"]["secret"] = "[REDACTED]"
        redactions.append({"scope": "codebuild", "field": "webhook.secret"})
    for source in [project.get("source", {})] + project.get("secondarySources", []):
        auth = source.get("auth", {})
        if auth.get("resource") and not auth["resource"].startswith("arn:"):
            auth["resource"] = "[REDACTED]"
            redactions.append({"scope": "codebuild", "field": "source.auth.resource"})
    bucket = None
    for item in project.get("environment", {}).get("environmentVariables", []):
        if item.get("name") == "OUTPUT_BUCKET" and item.get("type") == "PLAINTEXT":
            bucket = item.get("value")
        else:
            item["value"] = "[REDACTED]"
            redactions.append({"scope": "codebuild", "variable": item.get("name")})
    if project.get("serviceRole"):
        roles.add(project["serviceRole"])
    state = read("stepfunctions", "describe_state_machine", stateMachineArn=workflow)
    resources["workflow"] = {
        "current": state,
        "pinned_version": read("stepfunctions", "describe_state_machine", stateMachineArn=version),
        "tags": read("stepfunctions", "list_tags_for_resource", resourceArn=workflow),
    }
    if state.get("roleArn"):
        roles.add(state["roleArn"])
    api = read("apigatewayv2", "get_api", ApiId=args.api_id)
    resources["http_api"] = {
        "configuration": api,
        "routes": collect("apigatewayv2", "get_routes", "Items", ApiId=args.api_id),
        "integrations": collect("apigatewayv2", "get_integrations", "Items", ApiId=args.api_id),
        "stages": collect("apigatewayv2", "get_stages", "Items", ApiId=args.api_id),
        "authorizers": collect("apigatewayv2", "get_authorizers", "Items", ApiId=args.api_id),
    }
    checkpoint("workflow_project_and_api")

    resources["tables"] = {}
    for name in TABLES:
        resources["tables"][name] = {
            "description": read("dynamodb", "describe_table", TableName=name),
            "ttl": read("dynamodb", "describe_time_to_live", TableName=name),
            "backups": read("dynamodb", "describe_continuous_backups", TableName=name),
        }
    if isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        resources["bucket"] = {"name": bucket}
        for operation, absent in (
            ("get_bucket_location", ()),
            ("get_bucket_versioning", ()),
            ("get_bucket_encryption", ("ServerSideEncryptionConfigurationNotFoundError",)),
            ("get_public_access_block", ("NoSuchPublicAccessBlockConfiguration",)),
            ("get_bucket_ownership_controls", ("OwnershipControlsNotFoundError",)),
            ("get_bucket_policy", ("NoSuchBucketPolicy",)),
            ("get_bucket_lifecycle_configuration", ("NoSuchLifecycleConfiguration",)),
            ("get_bucket_tagging", ("NoSuchTagSet",)),
        ):
            resources["bucket"][operation] = read(
                "s3", operation, absent=absent, Bucket=bucket, ExpectedBucketOwner=account)
    else:
        errors.append({"service": "codebuild", "operation": "batch_get_projects",
                       "code": "OutputBucketUnavailable"})
    for entry in resources["tables"].values():
        arn = entry["description"].get("Table", {}).get("TableArn")
        if arn:
            entry["tags"] = read("dynamodb", "list_tags_of_resource", ResourceArn=arn)
    checkpoint("storage_configuration")

    resources["roles"] = {}
    for arn in sorted(roles):
        if not arn.startswith(f"arn:aws:iam::{account}:role/"):
            errors.append({"service": "iam", "operation": "get_role",
                           "code": "ReferencedRoleOutsideCurrentAccount"})
            continue
        name = arn.rsplit("/", 1)[1]
        inline_names = collect("iam", "list_role_policies", "PolicyNames", RoleName=name)
        resources["roles"][name] = {
            "role": read("iam", "get_role", RoleName=name),
            "inline_policies": {
                policy_name: read("iam", "get_role_policy", RoleName=name, PolicyName=policy_name)
                for policy_name in inline_names
            },
            "attached_policies": collect(
                "iam", "list_attached_role_policies", "AttachedPolicies", RoleName=name),
        }
    checkpoint("referenced_roles")

    resources["logs"] = []
    for name in ["/aws/lambda/" + name for name in FUNCTIONS] + ["/aws/codebuild/" + PROJECT]:
        groups = collect("logs", "describe_log_groups", "logGroups", logGroupNamePrefix=name)
        matches = [item for item in groups if item["logGroupName"] == name]
        if len(matches) != 1:
            errors.append({"service": "logs", "operation": "describe_log_groups",
                           "code": "ExpectedLogGroupUnavailable"})
        resources["logs"].extend(matches)
    resources["alarms"] = read("cloudwatch", "describe_alarms",
                               AlarmNames=list(ALARMS), AlarmTypes=["MetricAlarm", "CompositeAlarm"])
    resources["alerts"] = {
        "topic": read("sns", "get_topic_attributes", TopicArn=topic),
        "tags": read("sns", "list_tags_for_resource", ResourceArn=topic),
        "subscriptions": collect("sns", "list_subscriptions_by_topic", "Subscriptions", TopicArn=topic),
    }
    for subscription in resources["alerts"]["subscriptions"]:
        arn = subscription.get("SubscriptionArn", "")
        if arn.startswith("arn:"):
            subscription["attributes"] = read(
                "sns", "get_subscription_attributes", SubscriptionArn=arn)
    checkpoint("logs_and_alerts")

    packages = []
    for path in sorted(base.glob("**/package*.json")):
        if not path.resolve().is_relative_to(base.resolve()) or path.stat().st_size > 128 * 1024:
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError, OSError):
            continue
        if isinstance(body, dict) and re.fullmatch(r"[0-9a-f]{64}", str(body.get("sha256", ""))):
            manifest_keys = {
                "schema_version", "operation", "component", "live_inference",
                "package_source_commit", "source_commit", "build_id",
                "runtime", "handler", "bucket", "key", "version_id", "sha256", "bytes",
            }
            packages.append({"private_path": str(path), "manifest": {
                key: value for key, value in body.items() if key in manifest_keys
            }})
    resources["package_manifests"] = packages
    package_matches = {}
    for name, entry in functions.items():
        digest = entry["configuration"].get("CodeSha256", "")
        try:
            digest = base64.b64decode(digest, validate=True).hex()
        except ValueError:
            digest = ""
        package_matches[name] = [
            item["private_path"] for item in packages
            if digest and item["manifest"]["sha256"] == digest
        ]
    snapshot["package_matches"] = package_matches
    snapshot["finished_at"] = datetime.now(timezone.utc).isoformat()
    snapshot["status"] = "CAPTURED" if not errors else "PARTIAL"
    save()
    digest = hashlib.sha256((directory / "snapshot.json").read_bytes()).hexdigest()
    summary = {
        "SnapshotStatus": snapshot["status"],
        "Functions": len(functions), "Tables": len(resources["tables"]),
        "ReferencedRoles": len(resources["roles"]),
        "Routes": len(resources["http_api"]["routes"]),
        "LogGroups": len(resources["logs"]),
        "MetricAlarms": len(resources["alarms"].get("MetricAlarms", [])),
        "LambdaPackagesMatched": sum(bool(value) for value in package_matches.values()),
        "RedactedVariables": len(redactions), "ReadErrors": errors,
        "SubmissionsEnabled": False,
        "PrivateDirectory": str(directory), "SnapshotSha256": digest,
        "AWSChangesMade": False, "TerraformStateChanged": False,
    }
    path = directory / "summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps(summary, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
