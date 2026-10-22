"""CloudShell read-only preflight; render private CPU submission configuration.

Does not publish a workflow version, deploy resources, or start a build.
Run without --workflow-version-arn first to prepare the version publication.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

PROJECT = "legal-llm-cpu-check"
WORKFLOW = "legal-llm-cpu-prepare"
REPOSITORY = "https://github.com/yash-rai-93/Legal-LLM-Fine-Tuning-and-MLOps"


def aws_json(*args):
    result = subprocess.run(
        ["aws", *args, "--region", "us-east-1", "--output", "json", "--no-cli-pager"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Read-only AWS preflight request failed; inspect this resource in the console")
    return json.loads(result.stdout)


def validate_workflow(description, expected, account):
    if (description.get("type") != "STANDARD"
            or description.get("roleArn") != f"arn:aws:iam::{account}:role/legal-llm-cpu-workflow-role"
            or json.loads(description["definition"]) != expected):
        raise ValueError("Workflow differs from the reviewed fixed CPU definition")


def validate_project(project, expected):
    params = expected["States"]["PrepareAndPublish"]["Parameters"]
    environment = project.get("environment", {})
    if (project.get("name") != PROJECT or project.get("concurrentBuildLimit") != 1
            or project.get("autoRetryLimit") != 0
            or not 0 < project.get("timeoutInMinutes", 0) <= 15
            or not 0 < project.get("queuedTimeoutInMinutes", 0) <= 5
            or environment.get("computeType") != "BUILD_GENERAL1_SMALL"
            or environment.get("privilegedMode") is not False):
        raise ValueError("CPU project limits must be small compute, concurrency 1, retries 0, build <=15m and queue <=5m")
    source = project.get("source", {})
    if (source.get("type") != "GITHUB"
            or source.get("location", "").rstrip("/").removesuffix(".git") != REPOSITORY
            or source.get("buildspec") != params["BuildspecOverride"]):
        raise ValueError("Unexpected CPU project source/default buildspec")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", required=True)
    parser.add_argument("--workflow-version-arn")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+", args.api_id):
        raise ValueError("Use the actual HTTP API ID")
    root = Path(__file__).resolve().parents[1]
    account = aws_json("sts", "get-caller-identity")["Account"]
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Invalid AWS account")
    base_arn = f"arn:aws:states:us-east-1:{account}:stateMachine:{WORKFLOW}"
    arn = args.workflow_version_arn or base_arn
    if args.workflow_version_arn and not re.fullmatch(re.escape(base_arn) + r":[1-9][0-9]*", arn):
        raise ValueError("Use a numeric version of this account's CPU workflow")
    expected = json.loads((root / "infra/serverless-demo/stepfunctions/cpu-prepare.asl.json").read_text().replace(
        "__AWS_ACCOUNT_ID__", account,
    ))
    description = aws_json("stepfunctions", "describe-state-machine", "--state-machine-arn", arn)
    validate_workflow(description, expected, account)
    projects = aws_json("codebuild", "batch-get-projects", "--names", PROJECT)
    if projects.get("projectsNotFound") or len(projects.get("projects", [])) != 1:
        raise ValueError("CPU project unavailable")
    validate_project(projects["projects"][0], expected)
    api = aws_json("apigatewayv2", "get-api", "--api-id", args.api_id)
    stage = aws_json("apigatewayv2", "get-stage", "--api-id", args.api_id, "--stage-name", "$default")
    settings = stage.get("DefaultRouteSettings", {})
    if (api.get("ProtocolType") != "HTTP" or api.get("DisableExecuteApiEndpoint") is True
            or stage.get("AutoDeploy") is not True
            or not 0 < settings.get("ThrottlingRateLimit", 0) <= 1
            or not 0 < settings.get("ThrottlingBurstLimit", 0) <= 2):
        raise ValueError("Require the existing default-stage HTTP API with rate <=1/s and burst <=2")
    # Private configuration survives a CloudShell /tmp reset.
    os.umask(0o077)
    directory = Path.home() / "legal-llm-deploy/submission-api"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    def write(name, body):
        path = directory / name
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
    write("reviewed-definition.json", expected)
    if not args.workflow_version_arn:
        revision = description.get("revisionId")
        if not isinstance(revision, str) or not revision:
            raise ValueError("Workflow revision unavailable")
        write("publish-version.json", {
            "stateMachineArn": base_arn, "revisionId": revision,
            "description": "Reviewed fixed CPU preparation with verified run registration",
        })
        print("Read-only preflight passed. Prepared publish-version.json; no version was published.")
    else:
        policy_root = root / "infra/serverless-demo/submission-api"
        for source, destination in (("execution-policy.template.json", "execution-policy.json"),
                                    ("caller-policy.template.json", "caller-policy.json")):
            body = (policy_root / source).read_text().replace("__AWS_ACCOUNT_ID__", account)
            body = body.replace("__WORKFLOW_VERSION_ARN__", arn).replace("__HTTP_API_ID__", args.api_id)
            write(destination, json.loads(body))
        write("environment.disabled.json", {"Variables": {
            "HTTP_API_ID": args.api_id, "SUBMISSION_TABLE": "legal-llm-cpu-submissions",
            "WORKFLOW_VERSION_ARN": arn, "SUBMISSIONS_ENABLED": "false",
        }})
        print("Published workflow version verified; policies and DISABLED environment rendered.")
    print("Private configuration directory:", directory)
    print("No deployment, workflow execution or processing build was started.")


if __name__ == "__main__":
    main()
