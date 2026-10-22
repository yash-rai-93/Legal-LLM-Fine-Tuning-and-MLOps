# CPU monitoring: prepared configuration

The account owner supplied a read-only inventory showing no matching project
alarms. All five inspected project log groups already have seven-day retention:
the registration, status, artifact and submission Lambdas, and CPU CodeBuild.
Retain these settings.

This monitoring configuration is prepared, not yet cloud deployed or delivery
verified. The seven-check API regression passed with submissions disabled.
All three workload admissions remain used; monitoring does not replenish them.

## Three alarms

| Alarm | Signal | Threshold in five minutes |
| --- | --- | --- |
| `legal-llm-cpu-workflow-failures` | Sum of AWS/States ExecutionsFailed, ExecutionsTimedOut and ExecutionsAborted for the base CPU state-machine ARN | At least one |
| `legal-llm-cpu-lambda-errors` | Sum of AWS/Lambda Errors for the four CPU functions, each filtered by FunctionName | At least one |
| `legal-llm-cpu-api-server-errors` | AWS/ApiGateway `5xx`, filtered by ApiId | At least three |

All use standard-resolution metrics, Sum, a 300-second period, one evaluation
period and missing data treated as not breaching. Only ALARM transitions publish
to the dedicated standard SNS topic `legal-llm-cpu-alerts`. There are no job
start, recovery, Lambda, scaling, OK or insufficient-data actions. The SNS topic
policy permits CloudWatch publish only from the three exact alarm ARNs in the
same account. Account administration uses an explicit list of supported SNS
topic-policy actions on this topic. SNS topic policies do not accept the
`sns:*` wildcard supplied in the first prepared revision. The first real
CreateTopic attempt rejected that policy before the subscription call; topic,
alarm deployment and delivery still require successful AWS verification.

The workflow alarm uses the base ARN dimension, which covers executions started
through the published version. No version-specific metric is added to the sum.
Lambda Errors covers invocation failures, not errors handled as HTTP responses;
the API alarm complements it. Deliberate disabled-POST 503 responses also count
toward API `5xx`; use status/metadata GET requests for disabled-mode demos.
These alarms do not cover every failure, throttling, stuck admission, missing
metric or silent application defect. Sparse/best-effort metrics are not an
authoritative run ledger; use execution, child-build and submission records.

There are eight underlying alarm metrics across the three alarms. At an
illustrative standard alarm rate of $0.10 per metric-month, that is approximately
$0.80/month before applicable free allowances/credits; SNS usage is separate.
This is an estimate, not a live billing result or a spending cap. No custom
metrics, detailed API route metrics, dashboards or continuous poller are added.

## Prepare privately in CloudShell

Use the feature commit containing these files. This is an administrative
preflight, not an application test or package build:

```bash
python3 setup/prepare_cpu_monitoring.py --api-id ACTUAL_API_ID
```

The script reads identity, all four Lambda configurations, the configured
numeric Standard workflow version, HTTP API, relevant topic and alarm settings.
It requires submissions disabled and the API/workflow to match the submission
configuration. Existing topics or alarms with different settings stop preflight
rather than being overwritten. It renders requests under the private directory
`~/legal-llm-deploy/monitoring` with restricted file permissions. It does not
create resources, subscribe recipients, send messages or start a build.

Review `create-topic.json` and the three alarm request files before applying.
Actual account/API identifiers, SNS ARNs, email endpoints and audit files remain
private; do not commit rendered configuration.

## Deploy and verify delivery

1. Create the dedicated topic from the reviewed `create-topic.json`; its scoped
   publish policy is supplied at creation. Do not change any unrelated topic.
2. Subscribe an email address chosen by the operator. Enter it privately in
   CloudShell, not in source. AWS sends a confirmation email; the recipient
   must confirm. List subscriptions without printing endpoints and require the
   selected email subscription to be confirmed before enabling alarm actions.
   Adding or changing a recipient is a separate operator decision.
3. Apply only the three reviewed `put-metric-alarm` request files, then describe
   them and compare dimensions, expressions, thresholds, missing-data handling
   and exact topic action. Preserve CPU runtime configuration and the admission
   counter.
4. For one controlled delivery check, confirm the chosen alarm is not already
   ALARM and inspect recent metric state. Use `SetAlarmState` to transition
   that alarm to ALARM with a clear synthetic monitoring-test reason. This invokes
   the configured SNS action without submitting a CPU workload or publishing
   fake workload metrics. Metric evaluation will restore the actual state.
   If the alarm is already ALARM, investigate it first.
5. Require the email to arrive and inspect the matching alarm action history.
   Save a private audit containing the test reason/time, alarm and successful
   action result. Subscription confirmation alone does not establish delivery.
   A synthetic state change tests the notification path; it does not establish
   that a real workload fault crossed the metric threshold.

Deployment, confirmed subscription, exact alarm read-back and delivered test
notification remain pending until actual AWS evidence is supplied. Do not label
this monitoring milestone complete based on generated configuration.

## Retire this monitoring stage

After exporting any needed audit, disable/delete only these three alarms.
Remove the dedicated subscription/topic only after confirming they have no
other consumers. Keep seven-day log retention and the existing CPU resources.
Alarm deletion does not erase prior audit files. This is a separate cleanup
procedure; no cleanup has been executed.

## Primary references

- [Step Functions metrics and dimensions](https://docs.aws.amazon.com/step-functions/latest/dg/procedure-cw-metrics.html)
- [HTTP API metrics](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-metrics.html)
- [Metric math alarms](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Create-alarm-on-metric-math-expression.html)
- [Supported SNS topic-policy actions](https://docs.aws.amazon.com/sns/latest/dg/sns-access-policy-language-api-permissions-reference.html)
- [Alarm notifications and SNS subscriptions](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html)
- [Synthetic alarm-state testing](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_SetAlarmState.html)
- [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)
