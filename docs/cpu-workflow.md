# Manual CPU workflow: next deployment milestone

This incremental milestone wraps the existing verified CodeBuild project in
Step Functions Standard. DynamoDB, manifest-validator Lambda, authenticated
API, held-out test recovery and Terraform resource ownership remain pending.
Do not apply the original Terraform directory. Record the resources below
and import them deliberately before a later Terraform stack manages them.

## Definition and boundaries

- State machine: `legal-llm-cpu-prepare`, Standard, in `us-east-1`.
- Execution role: `legal-llm-cpu-workflow-role`.
- Inline policy: `LegalLlmCpuWorkflow`.
- Existing build project: `legal-llm-cpu-check`.
- Source version: verified immutable commit
  `8a1df10ea83cb9f5c476e849491cd427651c1c39`.
- Buildspec: `buildspecs/cpu-publish.yml`.
- Bucket: `yash-legal-llm-cpu-20261022`, private, versioned.
- Queue limit: 5 minutes; build: 15 minutes; task: 25 minutes; workflow: 30 minutes.
- Small compute, privileged mode off, no workflow Retry, CodeBuild auto-retries off.
- Manual operation only, no API/schedule/webhook/model download.

The task uses `codebuild:startBuild.sync` to wait for build completion.
A failed build fails the execution. Successful output identifies the build,
source commit and S3 prefix. The build checks uploaded bytes before succeeding.
There is not yet a separate manifest validator or run index.
Workflow input cannot override commands, roles, project, bucket or source ref.
Use `{}` as input. Execution-derived idempotency is not global admission
control; start one execution at a time and retain build concurrency one.

The workflow role can manage only this CPU project and the EventBridge rule
required by the integration. It has no S3, IAM, SageMaker or EC2 permissions.
The separate CodeBuild role retains its source, logs and scoped S3 permissions.

## 1. Prepare and validate in AWS CloudShell

Use the browser console in us-east-1. In the existing clean cloud checkout:

```bash
cd ~/Legal-LLM-Fine-Tuning-and-MLOps
git fetch origin feat/aws-cpu-deployment
git switch --detach origin/feat/aws-cpu-deployment
bash setup/prepare_cpu_workflow.sh
```

This renders account-specific JSON in a new temporary directory and invokes
AWS's definition validator. It creates no cloud resources and starts no build.
Require `"result": "OK"` and review warnings. Validation checks the definition,
not effective IAM permissions or actual build behavior.
Report errors rather than adding broad managed policies.

Record the printed directory. Replace the example path below with that value:

```bash
workflow_dir=/tmp/legal-cpu-workflow.EXAMPLE
cat "$workflow_dir/trust-policy.json"
cat "$workflow_dir/execution-policy.json"
cat "$workflow_dir/definition.json"
```

These configuration documents contain your account ID. Keep the generated
files out of the public repository.

## 2. Create the scoped role and state machine

After reviewing the exact generated files, run in CloudShell:

```bash
aws iam create-role \
  --role-name legal-llm-cpu-workflow-role \
  --assume-role-policy-document "file://$workflow_dir/trust-policy.json" \
  --tags Key=Project,Value=legal-llm-cpu \
  --no-cli-pager

aws iam put-role-policy \
  --role-name legal-llm-cpu-workflow-role \
  --policy-name LegalLlmCpuWorkflow \
  --policy-document "file://$workflow_dir/execution-policy.json" \
  --no-cli-pager

aws stepfunctions create-state-machine \
  --region us-east-1 \
  --cli-input-json "file://$workflow_dir/create-state-machine.json" \
  --no-cli-pager
```

These commands create the role, inline policy and workflow; they do not start
a build. Save the state-machine ARN. IAM propagation can briefly delay role
assumption; inspect the trust policy and retry only workflow creation after
propagation. On an existing name, inspect the resource before updating it.

Use Standard execution history at this milestone; additional state-machine
CloudWatch logging is not enabled. CodeBuild retains its existing logs.
Set that CodeBuild log group's retention to seven days.

## 3. One manual end-to-end run

In Step Functions, open legal-llm-cpu-prepare and start one uniquely named
execution with `{}` as input. This starts one finite paid CodeBuild job.
Credits and normal Budget alerts do not hard-cap charges. Do not redrive
during initial verification because it can run another build.

Acceptance:

1. Workflow success occurs after CodeBuild success.
2. Output has build_status SUCCEEDED, the pinned commit, live_inference false,
   and the expected bucket/prefix.
3. That new prefix contains train.jsonl, validation.jsonl and manifest.json.
4. Manifest counts are 423 train and 61 validation documents; semantic hashes
   match the earlier run. This is not a held-out test or model evaluation.

Record the execution ARN, build ID and prefix privately. After a failed,
stopped or timed-out execution, check CodeBuild for an active child job and
stop it if needed. Cancellation is best effort; the coordinator stopping
does not prove the build stopped. Its own timeout is a separate bound.

## Cleanup and inventory

First stop active executions and confirm their builds have ended. Delete
only this exact state machine, its dedicated inline policy and role.
Preserve the bucket, outputs and existing CodeBuild project.
The integration may create StepFunctionsGetEventForCodeBuildStartBuildRule,
an account-shared EventBridge rule; do not delete it if another workflow uses it.
Do not claim Terraform owns these manually created resources until imported.

## References

- [Integration and IAM](https://docs.aws.amazon.com/step-functions/latest/dg/connect-codebuild.html)
- [Definition validation](https://docs.aws.amazon.com/cli/latest/reference/stepfunctions/validate-state-machine-definition.html)
- [Execution-role trust](https://docs.aws.amazon.com/step-functions/latest/dg/procedure-create-iam-role.html)
- [Build overrides](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html)
