# Read-only dataset, prompt and evaluation API

Status: source prepared; CodeBuild validation, deployment and live authorization
checks remain pending. The existing run-status API remains the deployed service.

## Scope

Add three AWS_IAM GET routes to the existing HTTP API through a separate thin
Python Lambda:

| Route | Result |
| --- | --- |
| `GET /datasets` | Registered train, validation and test metadata; document counts and versioned references |
| `GET /prompts` | Registered prompt identity, digest and artifact reference; no prompt body |
| `GET /evaluations/{evaluation_id}` | One approved historical benchmark replay, request accounting, subset coverage and report references |

Every response states `live_inference: false`. These routes cannot trigger
builds, training or inference. They return selected fields from two approved
JSON manifests. Contract bodies, historical predictions and prompt content
are excluded. The held-out reference contains 123 documents; historical
requests cover only the benchmark subset. Missing immutable generating model
and adapter revisions prevent a controlled baseline/fine-tuning comparison.

The approved configuration is private deployment data. Git contains only a
placeholder example and synthetic test fixtures. Do not commit configuration,
receipts, rendered IAM policies or deployment outputs.

## Prepare private configuration in CloudShell

Use the deployment feature branch at an immutable reviewed commit. Record the
catalog and evaluation root manifest keys, actual S3 version IDs and SHA-256
digests from the successful cloud runs. A HEAD checksum metadata field alone
does not verify the downloaded bytes.

Create a private directory outside the repository and run:

```bash
umask 077
artifact_private_dir="$(mktemp -d /tmp/legal-artifact-private.XXXXXX)"
python3 -m src.cloud.artifact_api_config \
  --bucket "$artifact_bucket" \
  --catalog-key "$catalog_key" \
  --catalog-version "$catalog_version" \
  --catalog-sha256 "$catalog_sha256" \
  --evaluation-key "$evaluation_key" \
  --evaluation-version "$evaluation_version" \
  --evaluation-sha256 "$evaluation_sha256" \
  --output-dir "$artifact_private_dir"
```

Set the shell variables from your private AWS records first. This administration
step downloads only the two metadata manifests, checks current returned versions,
encryption and actual bytes, validates the three response projections, and checks
both producer builds succeeded at the recorded source commits. It writes a
private configuration and narrowly rendered execution policy outside Git, then
conditionally uploads the configuration to a fresh private S3 run prefix and
verifies its readback. It prints a receipt with the configuration key, version and
digest. No IAM, Lambda or API resources are created.

CloudShell `/tmp` is temporary. The verified configuration remains in private S3.
Retain the receipt privately; restore a private copy and re-render the policy if
the shell session is replaced. Do not paste credentials or signed headers.

## CodeBuild validation and package

Start one manual build in the existing CPU project with:

- An immutable source commit and `buildspecs/artifact-api-check.yml` override.
- The existing output bucket environment setting.
- `APPROVED_ARTIFACT_CONFIG_KEY`, `APPROVED_ARTIFACT_CONFIG_VERSION` and
  `APPROVED_ARTIFACT_CONFIG_SHA256` plaintext environment overrides from the
  private receipt. These are object identifiers, never credentials.
- The existing small CPU environment, retries disabled, a 15-minute build timeout
  and 5-minute queue timeout.

The build runs all thin-handler tests with synthetic fixtures, downloads the
private configuration and verifies its version/hash, checks both real manifests,
and exercises projections using injected API events. Those injected events do not
establish live IAM authorization.

The existing build role needs current `GetObject` access under its private
`runs/` prefix. No version-read permission is added to it: each current read must
return the approved version, otherwise the build fails. The runtime role instead
uses `GetObjectVersion` on the two exact manifest keys with version conditions.

The package contains only `artifact_api.py` and `approved_artifacts.json`.
The latter is assembled inside AWS from the verified private configuration.
The ZIP and package manifest are uploaded to the build's versioned S3 prefix and
read back for verification. Existing registry/status packages keep their
single-module layout. Packaging does not deploy resources.

## Deploy only after a successful build

1. Inspect the package manifest; verify the ZIP version and SHA-256 before use.
2. Pre-create the new Lambda log group with seven-day retention.
3. Create `legal-llm-cpu-artifact-api-role` with the checked-in Lambda trust
   policy and the private rendered execution policy. Its only grants are the
   two exact manifest versions and writes to its own pre-created log group.
4. Create `legal-llm-cpu-artifact-api`: Python 3.12,
   `artifact_api.lambda_handler`, 128 MiB, 10-second timeout, and
   `HTTP_API_ID` set to the existing API. Use the approved S3 ZIP version.
5. Wait for Active state and successful update; compare Lambda CodeSha256 with
   the verified package digest encoded as base64.
6. Add a new payload-2.0 integration on the existing API. Add only the three
   GET routes above with AWS_IAM. Grant API Gateway invocation only for those
   route paths and the owning source account.
7. Keep the existing stage throttle (one request/second, burst two) and existing
   `/runs/{run_id}` route. Do not create a Function URL, public authorization,
   VPC, schedule or provisioned concurrency.

Administrative IAM propagation can delay role assumption; inspect and retry a
failed resource creation once the role is visible instead of broadening trust.

## Live checks

The CloudShell client accepts `--resource datasets`, `--resource prompts`,
or `--resource evaluation --evaluation-id "$evaluation_id"`, plus the existing
`--endpoint`. It signs with CloudShell temporary credentials. Existing
`--run-id` behavior is retained.

Verify all three signed metadata GETs return 200, unsigned access returns 403,
an unknown canonical evaluation UUID returns 404, and the existing signed
run-status lookup still returns 200. The caller must separately have
`execute-api:Invoke`; the caller-policy template is a narrow optional example,
not a policy to attach blindly to the Lambda role.

Record successful evidence only after these checks. If removing this component,
remove only its three routes, integration, invoke permissions, Lambda and role.
Preserve the existing API, run-status route and historical artifacts.
