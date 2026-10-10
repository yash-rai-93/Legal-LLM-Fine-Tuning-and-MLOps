#!/bin/bash

promote_image() {
  local IMAGE_NAME=$1
  
  local SOURCE_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/dev-ci/${IMAGE_NAME}"
  local TARGET_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/dev/${IMAGE_NAME}"

  echo "Starting promotion for: ${IMAGE_NAME}..."

  aws ecr create-repository \
    --repository-name "dev/${IMAGE_NAME}" \
    --region "${AWS_REGION}" \
    || true # Ignores the error if the repo already exists

  docker pull "${SOURCE_REPO}:${IMAGE_TAG}"
  docker tag "${SOURCE_REPO}:${IMAGE_TAG}" "${TARGET_REPO}:${IMAGE_TAG}"
  docker push "${TARGET_REPO}:${IMAGE_TAG}"
  
  echo "Successfully promoted ${IMAGE_NAME}!"
  echo "-------------------------------------------"
}

IMAGES=(
  "raw-proc"
  "eval-notrain"
  # other images here
)

for img in "${IMAGES[@]}"; do
  promote_image "$img"
done