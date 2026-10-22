# Adopting the existing CPU deployment

The CPU workflow, IAM APIs and monitoring are manually deployed and verified.
The directory `infra/serverless-demo/` currently contains supporting policies
and configuration templates; it does not yet provide Terraform resource
ownership. Keep this distinction until imports and a saved plan are verified.

This stage begins with a private read-only snapshot. It does not create a
backend, import state, apply Terraform or perform cleanup. It does not establish
that an existing resource is absent from another Terraform state. Check existing
state ownership before assigning any resource to a new address.

## Capture current configuration

Run from the exact feature commit in AWS CloudShell:

```bash
python3 setup/capture_cpu_infrastructure.py --api-id ACTUAL_API_ID
```

The script uses temporary CloudShell credentials and read-only SDK operations.
It requires the submission API to remain disabled and its API ID and numeric
CPU workflow version to match. It reads only known CPU resources and the roles,
bucket and subscriptions referenced by them. It writes a timestamped directory
under `~/legal-llm-deploy/infrastructure/`, with private permissions:

- `snapshot.json`: live configurations, referenced policies, IDs and package
  manifest candidates, plus any read failures and redactions.
- `summary.json`: counts, status and snapshot SHA-256 suitable for sharing.

The snapshot checkpoints after each resource group. AWS timestamps are encoded
as ISO text. Unknown Lambda/project environment values, webhook secrets and
non-ARN source-auth resources are redacted; redactions must be resolved through
appropriate private configuration before claiming complete reproduction.
No session credentials, Lambda presigned download URLs, contract objects,
prediction files or DynamoDB application rows are copied. SNS endpoint and
subscription details remain private in the snapshot.

| Captured group | Scope |
| --- | --- |
| Lambda | Four CPU function configurations, concurrency, tags and resource policies |
| Workflow | Current CPU Standard definition, configured immutable numeric version and tags |
| CodeBuild | CPU project source/buildspec, role, environment, image, limits and settings |
| HTTP API | Existing API, routes, integrations, stages and authorizers |
| Storage | Two table descriptions/TTL/backup settings/tags and configured output bucket settings |
| IAM | Referenced roles, inline policies and managed-policy attachment references |
| Logs and alerts | Five known log groups, three alarm configurations, dedicated topic and subscriptions |
| Local package manifests | Saved private package manifests matched to deployed Lambda code hashes |

The script records absent optional bucket settings/resource policies separately
from failed reads. `PARTIAL` means one or more reads failed; share only the summary
and resolve the named service/operation errors. It never expands an IAM role or
changes a resource to make a read succeed. `CAPTURED` means the requested reads
completed; it does not mean the snapshot is a complete backup or a verified
recreation plan. Package-match counts and redactions are separate readiness
signals. A Lambda hash match identifies a candidate saved ZIP manifest; it does
not re-download or re-verify that S3 object version.

Keep the full snapshot outside the repository. Do not paste it into chat or
commit it: it contains private deployment configuration, IAM documents and email
subscription endpoints. Share only the final summary JSON.

## First live capture and package recovery

The account owner supplied a successful first CloudShell capture: four Lambdas,
two tables, six referenced roles, six HTTP routes, five log groups and three
metric alarms. ReadErrors was empty, no environment variables were redacted,
and submissions remained disabled. No AWS resource or Terraform state changed.
Only one Lambda matched a previously saved local package manifest, so full
package-pin recovery remains a prerequisite for the Terraform baseline.

From the feature commit containing the recovery script, run:

```bash
python3 setup/recover_cpu_package_pins.py --snapshot-dir ACTUAL_PRIVATE_SNAPSHOT_DIRECTORY
```

The script verifies the snapshot hash/account and current Lambda code hashes.
It lists only the configured bucket's `runs/` metadata, bounded to 5,000 listed
objects and 100 package-manifest candidates. It reads only matching package
manifest keys and selected thin ZIPs, never contract/prediction contents. It
uses manifests whose hashes match deployed code rather than selecting the newest
build. Each chosen candidate must have the correct component/schema, handler,
runtime, source commit, bucket, build-scoped key and non-null object version.

For each selected package, the script requires a completed successful CPU
CodeBuild producer at that exact source commit, repository, component buildspec
and output bucket. It reads the exact ZIP version, checks SSE-S3, byte length,
SHA-256 against the manifest and deployed Lambda, source metadata and bounded
ZIP contents/CRC. The artifact API's private approval JSON stays within the
verified ZIP. Legacy registry manifests without a component field are supported
only with the corresponding schema/operation/handler/key and all other checks.

Verified ZIPs, exact manifests and a recovery audit remain under a timestamped
private `package-recovery/` subdirectory. Only after all four packages verify
does the script write `package-pins.json` beside the original snapshot. The
snapshot itself is preserved. It rechecks live code hashes and disabled
submissions before completing. It never invokes or updates a Lambda, publishes
an object, starts a build or changes Terraform state.

Share only the final recovery summary. If INCOMPLETE/STOPPED, resolve the reported
gap; do not rebuild or substitute a different package merely to fill the pin.
The first real recovery run has now passed: six manifest candidates, four
functions pinned, four exact ZIP versions verified and four successful producer
builds verified. There were no unmatched functions or rejected candidates.
Submissions stayed disabled and no AWS resource or Terraform state changed.
The four-package pin gap identified by the first capture is closed.

## Prepare the private import map

Run the preparation helper from the feature commit containing it:

```bash
python3 setup/prepare_cpu_terraform_imports.py --snapshot-dir ACTUAL_PRIVATE_SNAPSHOT_DIRECTORY
```

It verifies the original snapshot checksum, all four package pins and recovered
ZIP bytes, then creates a new private `terraform-planning/` workspace. It makes
no AWS calls and does not install tools or run Terraform init/plan/import/apply.
The output is a planning scaffold, not an import-ready resource configuration.

| Private file | Purpose |
| --- | --- |
| `versions.tf.json` | Exact Terraform 1.16.6/AWS provider 6.67.0 pins, allowed account and isolated local planning backend |
| `imports.tf` | Existing resource addresses and provider-specific import IDs |
| `import-targets.json` | Explicit target inventory; duplicate type/ID and address checks |
| `deployed-package-pins.json` | Previously verified exact code references for Lambda configuration |
| `external-dependencies.json` | Immutable workflow version, confirmed subscriptions, referenced managed-policy definitions and source authorization where present |
| `preparation-summary.json` | Counts, toolchain version, architecture, free disk and stage status |

The known CPU resources and their imported subresources are selected from the
snapshot. IAM role policies/attachments and S3 bucket settings receive distinct
addresses; the later generated configuration must not also manage those same
settings through deprecated inline fields. Managed-policy definitions remain
external references until their ownership is reviewed. Email subscriptions and
the immutable published workflow version are recorded as external dependencies,
not silently recreated or republished. The original `Terraform/` stack and any
existing state files are untouched. The new local backend is only a planning
workspace; production state storage/locking and prior ownership remain review
decisions before adoption.

The helper reports whether Terraform is already installed; it invokes only
`terraform version -json` if found. Provider/config generation has not run at
this point. A subsequent plan may read live resources and produce private
generated HCL, provider locks and plan files. Keep provider downloads in an
ephemeral data directory if CloudShell home space is limited, while retaining
configuration, audit, lock file and any approved state durably.

The first real preparation returned READY with 52 import targets across 20
resource types, four verified Lambda packages and three external dependencies.
Terraform was not installed in that CloudShell session. Configuration generation
and the first plan have not run; no AWS resource or Terraform state changed.

## Initialize the pinned planning toolchain

Run the administrative helper against the workspace reported by preparation:

```bash
python3 setup/initialize_cpu_terraform.py --workspace-dir ACTUAL_PRIVATE_PLANNING_WORKSPACE
```

The helper downloads Terraform 1.16.6 for the current Linux architecture from
the official HashiCorp release endpoint and verifies its archive against the
release SHA256SUMS. This is an HTTPS checksum check, not an independent signature
verification. It extracts only the Terraform executable into a private temporary
directory and initializes the pinned AWS provider 6.67.0 and local planning
backend. It never runs plan/import/apply or changes AWS resources.

The binary and provider data live under /tmp to conserve CloudShell home space.
The provider lock, initialization log and toolchain audit remain privately in
the planning workspace. Temporary tool files can disappear when the session
ends; retain the lock/configuration and re-establish the pinned tools before
continuing in a new session. The helper refuses a recorded toolchain rather
than silently rerunning a failed or completed initialization; inspect the
private audit/log first. It requires the original scaffold and no state files.

Initialization does not generate resource configuration, adopt state or establish
a successful import-only plan. The first real initialization is pending.

### Lambda import nuance to resolve before the first adoption plan

AWS reports deployed code hashes but not the original S3 source location during
Lambda import. Keep the verified bucket/key/version pins, and compare the
provider's observed `code_sha256` to the deployed hash. The synthetic
`source_code_hash` does not recover the missing S3 origin. The pinned provider
treats changes to S3 source fields as code updates, so a matching ZIP hash alone
does not guarantee an import-only plan. Resolve these non-refreshable origin
fields explicitly before any adoption apply; do not accept a code update merely
to make an import pass or broadly ignore live configuration/code-hash drift.

Generated resource blocks must be reviewed and validated against the live
settings. A scaffold or an import-target count is not a successful plan, state
adoption, no-change result or recreation test.

## Build the isolated Terraform baseline after capture

1. Record the ownership boundary and the new state location. Check any existing
   state before adopting resources. Referenced/shared roles or managed policies
   may remain external dependencies rather than become managed resources.
2. Recover every exact Lambda ZIP object/version and verify its hash against
   the deployed code. Preserve the artifact API's packaged approval manifest.
   A function configuration/hash alone cannot recreate its code.
3. Define resource blocks from captured settings in a new isolated CPU stack.
   Pin Terraform/provider versions. Keep generated private variables, state and
   saved plans outside source; use encrypted/versioned state with locking.
4. Map each existing remote object to one Terraform address and prepare reviewed
   imports. Preserve route/integration IDs, numeric workflow pins, current role
   scopes, log retention, throttles, table data and disabled submissions. Treat
   unsupported immutable objects and externally managed policies explicitly.
5. Run Terraform formatting/validation and planning in AWS. The first adoption
   plan must contain imports with zero resource creates, updates, replacements
   or destroys. Resolve configuration drift before an adoption apply.
6. After state adoption, require a no-change plan and repeat the verified API/
   monitoring checks. Import readiness or a generated configuration file alone
   does not establish ownership or successful recreation.

No Terraform import/apply is included in the capture command. A saved plan and
exact state-ownership boundary are the reviewable next deliverables.

## Recreation and cleanup are separate acceptance work

The snapshot is configuration metadata, not a data backup. Restoring this
service requires its versioned S3 artifacts/packages, dataset and prompt approval
pins, registry/submission records and operator audits. In particular, preserve
the admission counter and idempotency records rather than resetting them through
recreation. Their recovery/export procedure has not yet been verified.

Recreation should first use deliberately isolated resources and a reviewed plan.
Existing production/demo names, the shared artifact bucket, table contents and
immutable workflow pins must not be replaced merely to test Terraform. The
cleanup inventory must identify shared dependencies and retained data before
any deletion. No cleanup, resource recreation, table export/restore, extra
processing admission or billing measurement is established by this snapshot.

## Primary references

- [Import existing Terraform resources](https://developer.hashicorp.com/terraform/cli/import/usage)
- [Configuration-driven imports](https://developer.hashicorp.com/terraform/language/import)
- [First import operation should preserve existing configuration](https://developer.hashicorp.com/terraform/tutorials/state/state-import)
- [Terraform configuration generation](https://developer.hashicorp.com/terraform/language/import/generating-configuration)
- [Pinned Terraform release](https://github.com/hashicorp/terraform/releases/tag/v1.16.6)
- [Pinned AWS provider release](https://github.com/hashicorp/terraform-provider-aws/releases/tag/v6.67.0)
- [Pinned provider Lambda implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.67.0/internal/service/lambda/function.go)
- [Lambda configuration API](https://docs.aws.amazon.com/lambda/latest/api/API_GetFunctionConfiguration.html)
- [CodeBuild project configuration](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_BatchGetProjects.html)
