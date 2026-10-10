## SETUP ==============================
# Run this file from main directory to set up project. Load environment variables first either through GHA or a .env file (local)

# Uncomment this for local development
# set -a
# source .env
# set +a

echo "IMAGE_TAG: ${IMAGE_TAG}"
echo "ENV: ${ENV}"

# Assume aws console has been authenticated
aws ecr get-login-password \
    --region us-east-1 | \
docker login \
    --username AWS \
    --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

# Test image ==============================
# Repo_name="${ENV}/my-busybox-aws"

# docker build \
#   -t my-busybox-docker:latest \
#   --build-arg HF_TOKEN="$HF_TOKEN" \
#   ./images/test_image

# aws ecr create-repository \
# --repository-name $Repo_name \
# --region us-east-1

# docker tag my-busybox-docker:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name}:${image_tag_short}"
# docker tag my-busybox-docker:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name}:test_abcd"

# docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name}:${image_tag_short}"
# docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name}:test_abcd"

# # MLRun Image ==============================
Repo_name_mlrun="${ENV}/mlrun-myjob"

docker build \
  --provenance=false \
  --platform=linux/amd64 \
  -t mlrun-myjob:latest \
  ./images/mlrun_function

aws ecr create-repository \
  --repository-name "$Repo_name_mlrun" \
  --region us-east-1

docker tag mlrun-myjob:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_mlrun}:${IMAGE_TAG}"
docker tag mlrun-myjob:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_mlrun}:latest"

docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_mlrun}:${IMAGE_TAG}"
docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_mlrun}:latest"

# Lambda traffic handler ==============================
echo "Building and pushing Lambda traffic handler..."
Repo_name_lambda="${ENV}/traffic-gateway"
# windows docker attaches metadata to image manifest which lambda doesn't understand
docker build \
  --provenance=false \
  --platform=linux/amd64 \
  --build-arg ENV="$ENV" \
  --no-cache \
  -t traffic-gateway:latest \
  ./images/lambda_gateway

aws ecr create-repository \
  --repository-name "$Repo_name_lambda" \
  --region us-east-1

docker tag traffic-gateway:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_lambda}:${IMAGE_TAG}"
docker tag traffic-gateway:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_lambda}:latest"

docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_lambda}:${IMAGE_TAG}"
docker push "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${Repo_name_lambda}:latest"

## SAGEMAKER TRAINING IMAGE (DON'T RUN THIS, IT TAKES 1 HOUR)  ==============================
# aws ecr get-login-password \
#     --region us-east-1 | \
# docker login \
#     --username AWS \
#     --password-stdin "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

# # Dockerfile is in /
# docker build -t smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124:latest .

# aws ecr create-repository \
# --repository-name smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124 \
# --region us-east-1

# docker tag smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124:latest "${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124:latest"

# docker push "${ACCOUNT_ID}".dkr.ecr.us-east-1.amazonaws.com/smhf-torch2.5.1-flash-trans5.3.0-gpul4-py311-cu124h:latest