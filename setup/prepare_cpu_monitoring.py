"""CloudShell read-only monitoring preflight and private request renderer.

Does not create alarms/topics, send notifications or start processing.
"""
import argparse
import json
import os
from pathlib import Path
import re

from prepare_cpu_submission import aws_json

REGION = "us-east-1"
TOPIC = "legal-llm-cpu-alerts"
FUNCTIONS = (
    "legal-llm-cpu-register",
    "legal-llm-cpu-status-api",
    "legal-llm-cpu-artifact-api",
    "legal-llm-cpu-submission-api",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]+", args.api_id):
        raise ValueError("Use the existing HTTP API ID")
    account = aws_json("sts", "get-caller-identity")["Account"]
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Invalid AWS account")
    workflow = f"arn:aws:states:{REGION}:{account}:stateMachine:legal-llm-cpu-prepare"
    topic = f"arn:aws:sns:{REGION}:{account}:{TOPIC}"
    configs = {}
    for name in FUNCTIONS:
        config = aws_json("lambda", "get-function-configuration", "--function-name", name)
        if (config.get("State") != "Active"
                or config.get("LastUpdateStatus") != "Successful"
                or config.get("FunctionArn") != f"arn:aws:lambda:{REGION}:{account}:function:{name}"):
            raise ValueError("CPU Lambda is unavailable or updating")
        configs[name] = config
    env = configs["legal-llm-cpu-submission-api"].get("Environment", {}).get("Variables", {})
    if (env.get("SUBMISSIONS_ENABLED") != "false" or env.get("HTTP_API_ID") != args.api_id
            or not re.fullmatch(re.escape(workflow) + r":[1-9][0-9]*", env.get("WORKFLOW_VERSION_ARN", ""))):
        raise ValueError("Require disabled submissions using this API and a numeric CPU workflow version")
    state = aws_json("stepfunctions", "describe-state-machine",
                     "--state-machine-arn", env["WORKFLOW_VERSION_ARN"])
    api = aws_json("apigatewayv2", "get-api", "--api-id", args.api_id)
    if state.get("type") != "STANDARD" or api.get("ProtocolType") != "HTTP":
        raise ValueError("Require the existing Standard workflow and HTTP API")

    root = Path(__file__).resolve().parents[1] / "infra/serverless-demo/monitoring"
    replacements = {
        "__ACCOUNT_ID__": account,
        "__STATE_MACHINE_ARN__": workflow,
        "__HTTP_API_ID__": args.api_id,
        "__ALERT_TOPIC_ARN__": topic,
    }

    def render(name):
        text = (root / name).read_text(encoding="utf-8")
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        if re.search(r"__[A-Z_]+__", text):
            raise ValueError("Unresolved configuration marker")
        return json.loads(text)

    alarms = render("alarms.template.json")
    policy = render("sns-policy.template.json")
    # SNS topic policies accept a narrower action set than IAM identity policies.
    topic_actions = {
        "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:AddPermission",
        "sns:RemovePermission", "sns:DeleteTopic", "sns:Subscribe",
        "sns:ListSubscriptionsByTopic", "sns:Publish",
    }
    for statement in policy["Statement"]:
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if (not actions or any(action not in topic_actions for action in actions)
                or statement.get("Resource") != topic):
            raise ValueError("SNS topic policy requires explicit supported actions on the exact topic")
    topics = aws_json("sns", "list-topics").get("Topics", [])
    topic_exists = any(item.get("TopicArn") == topic for item in topics)
    if topic_exists:
        attributes = aws_json("sns", "get-topic-attributes", "--topic-arn", topic)["Attributes"]
        if (attributes.get("KmsMasterKeyId") or attributes.get("FifoTopic") == "true"
                or json.loads(attributes.get("Policy", "{}")) != policy):
            raise ValueError("Existing alert topic differs from reviewed configuration; inspect before modifying")

    names = [item["AlarmName"] for item in alarms]
    existing = aws_json("cloudwatch", "describe-alarms", "--alarm-names", *names,
                        "--alarm-types", "MetricAlarm", "CompositeAlarm")
    if existing.get("CompositeAlarms"):
        raise ValueError("An expected metric alarm name is already a composite alarm")
    for found in existing.get("MetricAlarms", []):
        expected = next(item for item in alarms if item["AlarmName"] == found["AlarmName"])
        if any(found.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing monitoring alarm differs; inspect before overwriting")

    os.umask(0o077)
    directory = Path.home() / "legal-llm-deploy/monitoring"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def write(name, body):
        path = directory / name
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    write("create-topic.json", {
        "Name": TOPIC, "Attributes": {"Policy": json.dumps(policy)},
    })
    for alarm in alarms:
        write(alarm["AlarmName"] + ".json", alarm)
    write("inventory.json", {
        "region": REGION, "topic_arn": topic, "alarm_names": names,
        "submissions_enabled": False, "topic_already_exists": topic_exists,
        "existing_alarm_names": [item["AlarmName"] for item in existing.get("MetricAlarms", [])],
    })
    print(json.dumps({
        "Preflight": "PASSED", "AlarmCount": len(alarms),
        "UnderlyingAlarmMetrics": 8, "SubmissionsEnabled": False,
        "TopicAlreadyExists": topic_exists,
        "ConfigurationDirectory": str(directory),
        "DeploymentStarted": False,
    }, indent=2))


if __name__ == "__main__":
    main()
