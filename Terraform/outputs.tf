/*
These variables can be read in the deployment pipeline with the subprocess module. 
command: terraform output -json

Check utils.py
*/

output "bucket_name" {
  value = aws_s3_bucket.main_bucket.bucket
}

output "appconfig_app_id" {
  value = aws_appconfig_application.this.id
}

output "appconfig_env_id" {
  value = aws_appconfig_environment.this.environment_id
}

output "appconfig_confprof_cpid" {
  value = aws_appconfig_configuration_profile.this.configuration_profile_id
}

output "appconfig_deploystrat_rolling_id" {
  value = aws_appconfig_deployment_strategy.rolling_update.id
}

output "appconfig_deploystrat_direct_id" {
  value = aws_appconfig_deployment_strategy.direct_update.id
}

# output "iam_appconfig_cw_malarm_rollback" {
#   value = aws_iam_role.appconfig_exec_role.arn
# }

# output "appconfig_cw_malarm_1b_arn" {
#   value = aws_cloudwatch_metric_alarm.deployment_metric_carp_black.arn
# }

# output "appconfig_cw_malarm_1w_arn" {
#   value = aws_cloudwatch_metric_alarm.deployment_metric_carp_white.arn
# }

