locals {
  lambda_function_name = "${var.ENV}-model_gateway"
  #lambda_code_dir         = "../src/lambda" # this was for the lambda docker module
  appconfig_app_name      = "${var.ENV}-lambda_model_gateway"
  appconfig_env_name      = "${local.appconfig_app_name}-environment"
  appconfig_confprof_name = "${local.appconfig_app_name}-conf"
  s3_bucket_name          = "${var.ENV}-mlops-bucket-haviv"
}

# ==========================================
# S3
# ==========================================

resource "aws_s3_bucket" "main_bucket" {
  bucket = local.s3_bucket_name
}

resource "aws_s3_bucket_versioning" "versioning_example" {
  bucket = aws_s3_bucket.main_bucket.id
  versioning_configuration {
    status = "Enabled"
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "account_access" {
  bucket = aws_s3_bucket.main_bucket.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAccountPrincipals"
        Effect = "Allow"

        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }

        Action = "s3:*"

        Resource = [
          aws_s3_bucket.main_bucket.arn,       # The bucket itself
          "${aws_s3_bucket.main_bucket.arn}/*" # objects in the bucket
        ]
      }
    ]
  })
}

# ==========================================
# LAMBDA - RESOURCES (DOCKER + ECR + Lambda)
# ==========================================

/*
For the Appconfig extension layer: 
https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-versions.html#appconfig-integration-lambda-extensions-enabling-x86-64

Configuration URL:
https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-add.html
*/

resource "aws_lambda_function" "example" {
  function_name = local.lambda_function_name
  role          = aws_iam_role.lambda_exec_role.arn
  package_type  = "Image"
  image_uri     = "${var.ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/${var.ENV}/traffic-gateway:${var.IMAGE_TAG}"

  environment {
    variables = {
      configurationProfileURL = "http://localhost:2772/applications/${local.appconfig_app_name}/environments/${local.appconfig_env_name}/configurations/${local.appconfig_confprof_name}"
    }
  }
  timeout       = 450 # 7.5 mins
  architectures = ["x86_64"]
}
# image_config {
#   entry_point = ["/lambda-entrypoint.sh"]
#   command     = ["app.handler"]
# }


# ==========================================
# LAMBDA - IAM
# ==========================================

# Trust policy
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    effect  = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec_role" {
  name               = "${local.lambda_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Permissions policy
data "aws_iam_policy_document" "lambda_permissions" {
  # S3 Read Access (All buckets)
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket"
    ]
    resources = ["*"]
  }

  # SageMaker Endpoint Access (All endpoints)
  statement {
    effect = "Allow"
    actions = [
      "sagemaker:InvokeEndpoint"
    ]
    resources = ["*"]
  }

  # CloudWatch Logs Write Access
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }

  # Cloudwatch Metrics
  statement {
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData"
    ]
    resources = ["*"]
  }

  # AppConfig session
  statement {
    effect = "Allow"
    actions = [
      "appconfig:StartConfigurationSession",
      "appconfig:GetLatestConfiguration",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "lambda_permissions_attach" {
  name   = "${local.lambda_function_name}-permissions"
  role   = aws_iam_role.lambda_exec_role.name
  policy = data.aws_iam_policy_document.lambda_permissions.json
}

# ==========================================
# APPCONFIG - Resources
# ==========================================

resource "aws_appconfig_application" "this" {
  name = local.appconfig_app_name
}

resource "aws_appconfig_environment" "this" {
  name           = local.appconfig_env_name
  application_id = aws_appconfig_application.this.id

  # Attach CloudWatch metric alarms to Appconfig environment that will trigger rollback DURING deployment workflow with ac_client.start_deployment()
  monitor {
    alarm_arn      = aws_cloudwatch_metric_alarm.deployment_metric_carp.arn
    alarm_role_arn = aws_iam_role.appconfig_exec_role.arn
  }
}

resource "aws_appconfig_configuration_profile" "this" {
  application_id = aws_appconfig_application.this.id
  name           = local.appconfig_confprof_name
  location_uri   = "hosted"
}

# Deployment strategy is linear 25% increase in intervals across 20 minutes (5 mins each)
# These values should be environment specific, obviously shorter for development/test environments than staging/production environments
resource "aws_appconfig_deployment_strategy" "rolling_update" {
  name                           = "${local.appconfig_app_name}-rolling-update-deployment"
  deployment_duration_in_minutes = var.DEPLOYMENT_TIME_M # The total time for deployment
  final_bake_time_in_minutes     = var.BAKE_TIME_M       # The total time which to monitor alarms after deployment
  growth_type                    = "LINEAR"
  growth_factor                  = 25
  replicate_to                   = "NONE"
}

# Immediate deployment
resource "aws_appconfig_deployment_strategy" "direct_update" {
  name                           = "${local.appconfig_app_name}-direct-deployment"
  deployment_duration_in_minutes = 0
  final_bake_time_in_minutes     = 0 # The total time which to monitor alarms
  growth_type                    = "LINEAR"
  growth_factor                  = 100
  replicate_to                   = "NONE"
}


