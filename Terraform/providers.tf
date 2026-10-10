locals {
  aws_region = "us-east-1"
}

terraform {
  backend "s3" {
    bucket = "terraform-mlops-haviv"
    #key    = "project-haviv/${var.ENV}/terraform.tfstate"
    region = "us-east-1"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.28"
    }

  }

  required_version = ">= 1.2"
}

# Read aws creds from env variables for Github actions
provider "aws" {
  region = local.aws_region
  # shared_config_files      = [""]
  # shared_credentials_files = [""]
}


#########################################################################
# data "aws_ecr_authorization_token" "token" {}

# provider "docker" {
#   registry_auth {
#     address  = data.aws_ecr_authorization_token.token.proxy_endpoint
#     username = data.aws_ecr_authorization_token.token.user_name
#     password = data.aws_ecr_authorization_token.token.password
#   }
# }