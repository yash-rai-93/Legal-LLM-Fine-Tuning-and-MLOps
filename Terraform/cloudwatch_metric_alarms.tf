/*
https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarm-evaluation.html#alarm-evaluation-settings


Period is the length of time to use to evaluate the metric or expression to create each individual data point for an alarm. It is expressed in seconds.

Evaluation Periods is the number of the most recent periods, or data points, to evaluate when determining alarm state.

Datapoints to Alarm is the number of data points within the Evaluation Periods that must be breaching to cause the alarm to go to the ALARM state. The breaching data points don't have to be consecutive, but they must all be within the last number of data points equal to Evaluation Period.
*/

locals {
  cw_alarm_period        = 300 # 5 mins
  cw_alarm_eval_periods  = 1   # 2 periods of trigger required for alarm to sound off
  cw_datapoints_to_alarm = 1   # trigger if 5 data points breach within a 5 min period
  metric_namespace_dep   = "${var.ENV}-Short_contract_llm_drift_metrics"
}


######## IAM POLICY FOR APPCONFIG ROLLBACK BASED ON CLOUDWATCH ALARMS
# https://docs.aws.amazon.com/appconfig/latest/userguide/setting-up-appconfig.html

# Trust policy
data "aws_iam_policy_document" "appconfig_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    effect  = "Allow"
    principals {
      type        = "Service"
      identifiers = ["appconfig.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "appconfig_exec_role" {
  name               = "${var.ENV}-appconfig-cloudwatch-discovery-role"
  assume_role_policy = data.aws_iam_policy_document.appconfig_assume_role.json
}

# Permission policy
data "aws_iam_policy_document" "appconfig_permissions" {
  statement {
    effect = "Allow"
    actions = [
      "cloudwatch:DescribeAlarms"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "appconfig_permissions_attach" {
  name   = "${var.ENV}-appconfig-cw-discovery-role-permissions"
  role   = aws_iam_role.appconfig_exec_role.name
  policy = data.aws_iam_policy_document.appconfig_permissions.json
}

# ===============================================================================
# DEPLOYMENT ROLLBACK ALARMS
# ===============================================================================

# There are no dimensions
# Contract average rouge-3 fmeasure (CARF)
# resource "aws_cloudwatch_metric_alarm" "deployment_metric_carf" {
#   alarm_name          = "${var.ENV}-deployment-model-1"
#   comparison_operator = "LessThanOrEqualToThreshold"
#   period              = local.cw_alarm_period
#   evaluation_periods  = local.cw_alarm_eval_periods
#   datapoints_to_alarm = local.cw_datapoints_to_alarm
#   threshold           = 0.7
#   treat_missing_data  = "notBreaching" # alarm goes back to normal after the period passes

#   namespace   = local.metric_namespace_dep
#   metric_name = "CARF_3"
#   statistic   = "Average"
# }

resource "aws_cloudwatch_metric_alarm" "deployment_metric_carp" {
  alarm_name          = "${var.ENV}-deployment-model-1"
  comparison_operator = "LessThanOrEqualToThreshold"
  period              = local.cw_alarm_period
  evaluation_periods  = local.cw_alarm_eval_periods
  datapoints_to_alarm = local.cw_datapoints_to_alarm
  threshold           = 0.7
  treat_missing_data  = "notBreaching" # alarm goes back to normal after the period passes

  namespace   = local.metric_namespace_dep
  metric_name = "CARP_3"
  statistic   = "Average"
}

# ===============================================================================
# MODEL DRIFT ALARMS (LONG-TERM MONITORING)
# These should be manually added with every release and be unique to the endpoint_name 
# so as to prevent retrospective calculation with previous versions
# ===============================================================================

# CARP metric alarm
# resource "aws_cloudwatch_metric_alarm" "drift_metric_carp" {
#   alarm_name          = "${var.ENV}-monitoring-model-1"
#   comparison_operator = "LessThanOrEqualToThreshold"
#   period              = local.cw_long_alarm_period
#   evaluation_periods  = local.cw_alarm_eval_periods
#   datapoints_to_alarm = local.cw_datapoints_to_alarm
#   threshold           = 0.7
#   treat_missing_data  = "ignore" # alarm state is preserved

#   namespace   = local.metric_namespace_drift
#   metric_name = "CARP_rouge2"
#   statistic   = "Average"
# }