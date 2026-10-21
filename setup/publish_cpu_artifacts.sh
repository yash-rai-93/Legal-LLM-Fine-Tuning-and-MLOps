#!/usr/bin/env bash
# Run only in AWS CodeBuild after tests and data preparation succeed.
set -euo pipefail

: "${OUTPUT_BUCKET:?Set the OUTPUT_BUCKET CodeBuild environment variable}"
: "${CODEBUILD_BUILD_ID:?This publisher requires an AWS CodeBuild run}"
: "${AWS_DEFAULT_REGION:?The AWS CodeBuild region is required}"

artifact_dir="${1:?Supply the prepared artifact directory}"
run_id="${CODEBUILD_BUILD_ID##*:}"
if [[ ! "$run_id" =~ ^[a-zA-Z0-9-]+$ ]]; then
  echo "Unexpected CodeBuild run ID format" >&2
  exit 1
fi
run_prefix="runs/$run_id"

bucket_region="$(aws s3api get-bucket-location --bucket "$OUTPUT_BUCKET" --query LocationConstraint --output text)"
# S3 returns null/None for us-east-1.
if [[ "$bucket_region" == "None" || "$bucket_region" == "null" ]]; then
  bucket_region="us-east-1"
fi
if [[ "$bucket_region" != "$AWS_DEFAULT_REGION" ]]; then
  echo "Bucket and build must use the same region" >&2
  exit 1
fi
versioning="$(aws s3api get-bucket-versioning --bucket "$OUTPUT_BUCKET" --query Status --output text)"
if [[ "$versioning" != "Enabled" ]]; then
  echo "Enable S3 bucket versioning before publishing this run" >&2
  exit 1
fi

for artifact in train.jsonl validation.jsonl manifest.json; do
  if [[ ! -s "$artifact_dir/$artifact" ]]; then
    echo "Required output is absent or empty: $artifact" >&2
    exit 1
  fi
done

verify_dir="$(mktemp -d /tmp/legal-cpu-verify.XXXXXX)"
# Upload data first and the manifest last. A complete published run also
# requires the CodeBuild run to succeed; do not consume partial/failed runs.
for artifact in train.jsonl validation.jsonl manifest.json; do
  destination="s3://$OUTPUT_BUCKET/$run_prefix/$artifact"
  aws s3 cp "$artifact_dir/$artifact" "$destination" \
    --sse AES256 --only-show-errors --no-progress
  aws s3 cp "$destination" "$verify_dir/$artifact" \
    --only-show-errors --no-progress
  if ! cmp --silent "$artifact_dir/$artifact" "$verify_dir/$artifact"; then
    echo "Uploaded bytes do not match: $artifact" >&2
    exit 1
  fi
  # Log metadata only, never contract bodies or credentials.
  echo "Verified $destination"
  aws s3api head-object --bucket "$OUTPUT_BUCKET" \
    --key "$run_prefix/$artifact" \
    --query '{Bytes:ContentLength,Encryption:ServerSideEncryption,VersionId:VersionId}' \
    --output json
done
echo "Published and byte-verified all three outputs under s3://$OUTPUT_BUCKET/$run_prefix/"
# Files in verify_dir disappear with the ephemeral build environment.
