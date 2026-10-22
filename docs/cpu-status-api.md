# Authenticated status API: deployed and cloud-verified

This is the first HTTP API stage: GET /runs/{run_id} returns metadata for one
registered COMPLETED run. Processing status remains available in Step Functions.
POST /runs and dataset/prompt listing remain later stages. Submission requires
admission control and idempotency before it can be exposed safely.

## API contract

- Region: us-east-1; HTTP API payload format 2.0; default stage.
- Route: GET /runs/{run_id}, with AWS_IAM authorization from creation.
- Run ID: canonical build/run UUID stored in DynamoDB, rather than execution UUID.
- Example registered run: 1605eeda-3334-4fc4-a8f3-c5c047f75994.
- HTTP 200: completed run metadata, counts, provenance, S3 keys and object versions.
- HTTP 400: invalid UUID, unsupported format or query parameters.
- HTTP 403: IAM rejection; the handler also checks trusted IAM/API context.
- HTTP 404: route or registered run unavailable; 405 for a wrong method reaching the handler.
- HTTP 503: backend/schema failure with a sanitized response.
- Responses use application/json and Cache-Control: no-store.

The caller must have execute-api:Invoke on this route and sign with SigV4.
The handler permits only the configured HTTP_API_ID and requires the IAM
userArn in API Gateway's trusted request context. It does not trust caller
identity supplied in headers or the body. It returns an explicit field
allowlist and converts DynamoDB integer Decimals into JSON integers.

The dedicated role permits only GetItem on legal-llm-cpu-runs and writes to
its own log group. It cannot start CodeBuild/Step Functions, modify records,
read S3 objects or invoke model services. The API returns artifact references;
the existing S3 bucket and contract content remain private.

## Cloud check and package

Run one manual build using the immutable feature commit and
buildspecs/status-api-check.yml as a one-build override. Keep the CPU project's
default source/buildspec unchanged. The build runs all 28 cloud unit tests
(14 registry and 14 status API tests), then publishes only the status API ZIP.
These are injected-client fixtures. The successful cloud build and the separate
real API Gateway/IAM checks are recorded below.

The shared packager accepts an allowlisted --component status-api option.
Its default remains registry. The deployed registry package/code are unchanged.
Outputs go to runs/<package-build-UUID>/packages/cpu-status-api.zip and package.json.
Both uploads are byte-verified and have recorded version IDs and hashes.
No pip dependencies, models, infrastructure changes or GPU resources are used.

## Verified deployment evidence

Verified from account-owner-supplied AWS output on 22 October 2026 (IST).
Tests and packaging ran in AWS CodeBuild; the HTTP checks ran from CloudShell.

- Source: `6113880be73a3caa8bb483473336e016c7d3fd3b`.
- Build: `legal-llm-cpu-check:0ff4be99-7647-4a6d-a6b7-97a6d2dad3da`,
  SUCCEEDED / COMPLETED using `buildspecs/status-api-check.yml` (28 cloud tests).
- Package: `runs/0ff4be99-7647-4a6d-a6b7-97a6d2dad3da/packages/cpu-status-api.zip`
  in the existing private artifact bucket; 2101 bytes.
- Package object version: `43VJ3ac5_YnN9Zs9N6Ky_gUhPVurXliN`.
- Package SHA-256: `f16bd40d3ee37a2dc48f309358a912a44c8cba1a52a83deeca95040449d205fc`.
- Lambda: `legal-llm-cpu-status-api`, Active / Successful, Python 3.12.
  Reported CodeSha256 `8WvUDT7jei3EjzCTWKkSpEyMuhpSqD3uypUEBEnSBfw=`
  matches the manifest's hex checksum after base64 conversion.
- HTTP API: `legal-llm-cpu-status`; exact GET route confirmed with AWS_IAM.
- Stage: `$default`, auto-deploy enabled; rate 1 request/second, burst 2,
  detailed metrics disabled.
- Dedicated execution role and invocation permission were created using the
  scoped policies described below. Own log group retention was set to seven days.

| Real HTTP check | Observed result |
| --- | --- |
| Signed GET for `1605eeda-3334-4fc4-a8f3-c5c047f75994` | 200; COMPLETED, live_inference=false; train 423 documents / 7191 rows, validation 61 documents / 1037 rows |
| Unsigned GET for the same run | 403 |
| Signed GET for unregistered `00000000-0000-4000-8000-000000000001` | 404 |

The 200 response included the expected processor/registry commits, S3 keys,
object version IDs, AES256 encryption, sizes and checksums. The manifest
version was `efsfjDwxyPcvmTvJJ3UtRcJeKl88doXh`, matching the integrated
workflow result. No processing build was started by these HTTP reads.

These checks verify read-only retrieval and IAM enforcement for the tested
requests. Load/throttle behavior, backend-failure recovery, cleanup/recreation
and billing measurement remain unverified. POST submission, running/failed
run tracking, dataset/prompt listing and model inference are not deployed.
Keep account-specific API identifiers and generated policies in the private
resource inventory.

## Runtime resources and deployment order

Create only after the cloud check succeeds and the exact package is identified:

1. Create an empty HTTP API named legal-llm-cpu-status, protocol HTTP, project
   tag. Omit quick-create target/default route options. Record ApiId/ApiEndpoint.
2. Create /aws/lambda/legal-llm-cpu-status-api and set seven-day retention.
3. Create role legal-llm-cpu-status-api-role with the supplied Lambda trust
   document and rendered execution policy. No broad managed policy is needed.
4. Create Lambda legal-llm-cpu-status-api from the verified S3 object version:
   Python 3.12, handler status_api.lambda_handler, x86_64, 128 MiB, timeout 10s.
   Set RUN_TABLE=legal-llm-cpu-runs and HTTP_API_ID to the actual ApiId.
   Keep Function URL, VPC, schedules and provisioned concurrency unconfigured.
5. Create an AWS_PROXY Lambda integration with payload format 2.0 and a
   12-second integration timeout, then the exact GET route with AWS_IAM.
6. Grant API Gateway Lambda invocation only for this API/account and
   */GET/runs/* using a scoped Lambda resource-policy statement.
7. Create the $default stage with automatic deployment, rate 1 request/second
   and burst 2. Keep detailed metrics, custom domains and extra paid services off.

Templates use __AWS_ACCOUNT_ID__ and, for caller permissions, __HTTP_API_ID__.
Render them in CloudShell using the actual identifiers; keep generated
account-specific files out of public source control. Existing authorized
CloudShell identity can be used; no long-lived keys or new IAM user are needed.

## Test from CloudShell after deployment

The administration client setup/call_cpu_status_api.py restricts destinations
to default-stage us-east-1 execute-api endpoints, validates the run UUID,
blocks redirects, and signs with botocore's standard temporary credential chain.
It prints metadata/status, never credentials, signed headers or raw error bodies.
Use a CloudShell-only temporary environment with botocore if it is unavailable.
Application tests and packaging remain in CodeBuild.

```bash
python3 setup/call_cpu_status_api.py \
  --endpoint https://ACTUAL_API_ID.execute-api.us-east-1.amazonaws.com \
  --run-id 1605eeda-3334-4fc4-a8f3-c5c047f75994
```

Require HTTP 200 and expected counts/versions for the real registered run.
Repeat with --unsigned and require HTTP 403. That check intentionally exits
nonzero and should not be chained to a subsequent successful test with &&.
Also use a different valid unregistered UUID and require HTTP 404; invalid
UUIDs are rejected by the client before credentials are loaded.
Space requests to respect the low throttle; the client has no retry loop.

## Costs, scope and cleanup

Requests use API Gateway, finite Lambda invocations and DynamoDB reads.
Keep use manual and monitor actual service charges and remaining credits;
the quoted credit summary updates approximately daily and may lag recent runs.
No new-build trigger is exposed by this stage. Gateway throttling is not a
billing cap, and the $20 AWS Budget is an alert rather than an account limit.

Record API/integration/route/stage IDs, function/role/log group and permission
statement in the private inventory. To retire this stage, remove the API and
its scoped Lambda invocation permission, then the status Lambda/dedicated
role/policy; preserve the existing CPU workflow, registry, table and S3 data.
Retained logs follow their own policy. Terraform ownership and the bounded
submission API remain pending; do not apply the original model Terraform stack.

## Primary references

- [HTTP API IAM/SigV4](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-access-control-iam.html)
- [Lambda payload version 2.0](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-lambda.html)
- [AWS IAM event fields](https://github.com/aws/aws-lambda-go/blob/main/events/apigw.go)
- [SDK signing example](https://docs.aws.amazon.com/solutions/latest/spatial-data-management-on-aws/api-authentication.html)
