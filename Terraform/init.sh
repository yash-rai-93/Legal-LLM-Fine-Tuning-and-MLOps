# this file is for local development

terraform -chdir=Terraform/ init \
    -backend-config="backend/sand.hcl"