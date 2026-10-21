#!/usr/bin/env bash
# AWS CloudShell administration only: render files and ask AWS to validate ASL.
# This script does NOT create roles/state machines or start builds.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd -- "$script_dir/.." && pwd)"
output_dir="$(mktemp -d /tmp/legal-cpu-workflow.XXXXXX)"
# The optimized Step Functions integration does not accept AutoRetryLimitOverride.
# Require an explicitly disabled project-level retry policy before deployment.
retry_limit="$(aws codebuild batch-get-projects \
  --region us-east-1 --names legal-llm-cpu-check \
  --query 'projects[0].autoRetryLimit' --output text --no-cli-pager)"
if [[ "$retry_limit" != "0" ]]; then
  echo "CodeBuild autoRetryLimit must be explicitly set to 0 (reported: $retry_limit)." >&2
  echo "Run in CloudShell: aws codebuild update-project --region us-east-1 --name legal-llm-cpu-check --auto-retry-limit 0 --query project.autoRetryLimit --output text --no-cli-pager" >&2
  exit 1
fi
echo "Verified CodeBuild project autoRetryLimit: 0"

account_id="$(aws sts get-caller-identity --query Account --output text --no-cli-pager)"
if [[ ! "$account_id" =~ ^[0-9]{12}$ ]]; then
  echo "AWS did not return a valid account ID" >&2
  exit 1
fi

python3 - "$repo_dir" "$output_dir" "$account_id" <<'PY'
import json
import pathlib
import sys

repo_dir, output_dir = map(pathlib.Path, sys.argv[1:3])
account_id = sys.argv[3]
templates = repo_dir / "infra/serverless-demo/stepfunctions"
for source, target in (
    ("cpu-prepare.asl.json", "definition.json"),
    ("execution-policy.template.json", "execution-policy.json"),
    ("trust-policy.template.json", "trust-policy.json"),
):
    raw = (templates / source).read_text(encoding="utf-8")
    rendered = json.loads(raw.replace("__AWS_ACCOUNT_ID__", account_id))
    (output_dir / target).write_text(
        json.dumps(rendered, indent=2) + "\n", encoding="utf-8"
    )
request = {
    "name": "legal-llm-cpu-prepare",
    "type": "STANDARD",
    "roleArn": f"arn:aws:iam::{account_id}:role/legal-llm-cpu-workflow-role",
    "definition": (output_dir / "definition.json").read_text(encoding="utf-8"),
    "tags": [{"key": "Project", "value": "legal-llm-cpu"}],
}
(output_dir / "create-state-machine.json").write_text(
    json.dumps(request, indent=2) + "\n", encoding="utf-8"
)
PY

aws stepfunctions validate-state-machine-definition \
  --region us-east-1 --type STANDARD --severity WARNING \
  --definition "file://$output_dir/definition.json" \
  --output json --no-cli-pager > "$output_dir/validation.json"

python3 - "$output_dir/validation.json" <<'PY'
import json
import pathlib
import sys

result = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps(result, indent=2))
if result.get("result") != "OK":
    raise SystemExit("AWS definition validation failed; do not deploy.")
PY

echo "Prepared and AWS-validated workflow files: $output_dir"
echo "No resources created and no build started."
