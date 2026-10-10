# this file is for local development

set -a # export variables into child processes started by this shell
source .env
set +a # disable

terraform -chdir=Terraform/ apply \
    -var-file="sand.tfvars" \
    --auto-approve