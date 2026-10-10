# this file is for local development

set -a # export variables into child processes started by this shell
source .env
set +a # disable

terraform -chdir=Terraform/ validate \
    -var-file="sand.tfvars" \

terraform -chdir=Terraform/ plan \
    -var-file="sand.tfvars" \