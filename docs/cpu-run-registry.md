# CPU run registry: deployed and independently verified

The existing Step Functions/CodeBuild/S3 workflow remains deployed and unchanged.
This milestone deploys a thin Lambda that records successful, verified CPU
runs in DynamoDB. Supplied AWS output confirms first registration and an
idempotent duplicate invocation. The Step Functions integration update is
prepared; AWS definition validation, update and integrated smoke test remain pending.

## What the handler verifies

1. The event names the configured CPU project, bucket, immutable processor
   commit and the exact runs/<build UUID>/ prefix.
2. CodeBuild independently reports the same completed, successful GitHub build,
   resolved source commit, approved publication buildspec and output bucket.
3. The manifest has the expected schema, dataset attribution, timezone-aware
   creation time, train/validation splits and 17 hypothesis rows per document.
4. All three objects have non-null version IDs and SSE-S3 encryption.
5. Reads use the exact versions found by HeadObject. Both output SHA-256 hashes
   are recomputed by streaming bounded objects and compared with the manifest.
6. A conditional DynamoDB write creates the run once. A duplicate invocation
   returns the original record; a conflicting artifact/version fingerprint fails.

Only metadata is stored. Raw contracts and predictions are not logged or stored
in DynamoDB. Raw-input and semantic hashes are retained from the trusted
processor manifest; they are not independently recomputed from raw inputs.
Document content/schema validation remains the pinned, tested CPU build's job.
This handler does not generate predictions, measure model accuracy or register
models. At this stage only COMPLETED runs are indexed. Failed/running workflows
remain visible in Step Functions/CodeBuild, not as a complete DynamoDB run log.

## First: run cloud unit tests and package the handler

Run one manual build against the new immutable feature-branch commit with
buildspec override buildspecs/registry-check.yml. Keep the project's default
cpu-publish buildspec, existing source configuration and OUTPUT_BUCKET unchanged.
Keep small on-demand compute, timeout 15 minutes, queue timeout 5 minutes,
concurrency one and project autoRetryLimit zero. Native artifacts remain
NO_ARTIFACTS. No pip packages, models or GPU images are downloaded.

The build performs shell syntax checks and 14 fixture-based registry tests,
then creates a thin ZIP containing registry.py and publishes it alongside
package.json under runs/<package build UUID>/packages/ in the existing bucket.
It reads back both uploads, checks their versions/encryption and compares bytes.
The existing build role's scoped S3 permissions are sufficient.

The package manifest records its source commit, ZIP hash/version ID, handler
and intended runtime. Keep package-source and processor-source commits distinct:

| Setting | Value |
| --- | --- |
| Runtime | python3.12 |
| Handler | registry.lambda_handler |
| Package source | Commit printed by the successful package build |
| PROCESSOR_SOURCE_COMMIT | 8a1df10ea83cb9f5c476e849491cd427651c1c39 |
| REGISTRY_SOURCE_COMMIT | Same as the successful package source commit |
| OUTPUT_BUCKET | yash-legal-llm-cpu-20261022 |
| CODEBUILD_PROJECT | legal-llm-cpu-check |
| RUN_TABLE | legal-llm-cpu-runs |

The tests use injected fake clients and synthetic metadata bodies. They verify
failure contracts and idempotency, not real AWS permissions or model results.
Application tests and packaging execute in CodeBuild, not on the laptop.
Do not deploy a package if the cloud build or upload verification fails.

## Runtime resources: deployed after the cloud check

- DynamoDB table legal-llm-cpu-runs: string partition key run_id; no sort key
  or secondary indexes; PAY_PER_REQUEST, STANDARD class; project tag.
- Lambda legal-llm-cpu-register: Python 3.12, ZIP from the verified S3 object
  version, x86_64, 256 MiB, 60-second timeout; no Function URL or VPC.
- Pre-create /aws/lambda/legal-llm-cpu-register with seven-day log retention.
- Dedicated role legal-llm-cpu-register-role with the supplied trust document
  and the rendered execution-policy.template.json. No managed administrator
  policy, deployment rights, model-service rights or S3 writes.
- Allow the existing workflow role to invoke only this function using
  workflow-invoke-policy.template.json; retain its existing CodeBuild policy.

Templates use __AWS_ACCOUNT_ID__; render them with the signed-in account's
actual ID in CloudShell. Do not commit generated account-specific documents.
Runtime SDK calls have short connection/read timeouts and at most two attempts.
The managed Lambda runtime supplies boto3/botocore. No partial SDK dependency
bundle is shipped; runtime SDK versions are not pinned in this first package.

## Integration and acceptance after provisioning

First invoke the Lambda synchronously using the existing successful workflow
output, not a new CPU build. Expected existing build ID:
legal-llm-cpu-check:2ec431fc-340c-45f8-bb16-d93ab1c3aa89.
Require a COMPLETED record with 423 train / 61 validation documents, source
commit, object hashes and actual S3 version IDs. Invoke the same event again
and require created=false with the original registration timestamp.

Then add a Lambda invoke task after PrepareAndPublish in the Standard workflow,
passing the current task's small metadata output as Payload. Use a 90-second
task timeout and no automatic workflow Retry. Validate the updated definition
in AWS before updating the deployed workflow. Preserve the pinned processor
commit and existing build limits. Run one integrated smoke test afterward.

Acceptance also includes a rejected checksum/source mismatch with no successful
run record. No API or external trigger is exposed in this milestone.
Record each created resource and import deliberately before later Terraform
ownership. Billing measurement and complete failure/cancellation tests remain
pending; normal AWS Budget alerts do not hard-cap account spending.

## References

- [Conditional writes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html)
- [Versioned object reads and permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)
- [Python Lambda runtime and SDK](https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html)

## Verified cloud evidence

Account-owner-supplied output confirms the following milestones:

- Registry check/package build: `f30a91c1-05a2-4d21-be07-528d1c46c8ca`, SUCCEEDED.
- Package/registry source: `39d979d9fc0e40457b3ae81e9a9a3f445099fa61`.
- Deployed code SHA-256: `da7e2d52eaaab59426e036852c89f8388aeab58220ba3851af213da43112db08`, matching the package.
- Lambda Active/Successful, Python 3.12; DynamoDB table ACTIVE.
- First synchronous invocation: COMPLETED, created=true, 423 train / 61 validation documents.
- Second invocation with the same event: COMPLETED, created=false; original timestamp preserved.
- Registered run: `2ec431fc-340c-45f8-bb16-d93ab1c3aa89`.
- Manifest version: `mkoNLxw1Fr2e7DS0ATSmgUGWOkvnhXQr`.
- Original registration time (machine UTC): `2026-10-21T23:43:13.566949+00:00`.

See [workflow update instructions](cpu-workflow.md#update-the-existing-workflow-with-registration).
The live workflow still needs this update and its first integrated verification.
