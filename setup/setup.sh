# Run this file from main directory on non-local environments

# Uncomment for local dev environment
# set -a
# source .env
# set +a

echo "IMAGE_TAG: ${IMAGE_TAG}"
echo "ENV: ${ENV}"

echo "Running setup project"
python setup/setup_project.py

echo "Running build_functions.py"
python setup/build_functions.py