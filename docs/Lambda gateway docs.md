# Lambda traffic gateway functions:

- Reads prompt template from S3
- Combines prompt template with input, then formats into chatml template. Transformers library isn't necessary if we hardcode the format
- Enables model rolling updates by controlling traffic to model endpoints/- Inference component adapters

- Sends operations logs to cloudwatch
- Sends model drift metrics to cloudwatch (requires rouge-score library)
- Sends invocations and response data to data firehose, which goes to S3 (very large logs shouldn't be in Cloudwatch)

Variables are controlled by appconfig https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-creating-deployment-strategy.html

# Model deployment workflow
Assuming that infrastructure for lambda/appconfig/IAM is already created with TF

1. Create free-form configuration profile on configuration store with CreateHostedConfigurationVersion

https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-free-form-configurations-creating.html

https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-free-form-configurations-creating.html

2. Start deployment with StartDeployment strategy (requires id for: App, Env, DeployStrat, ConfProf. And ConfProfVer)

https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-deploying.html

https://docs.aws.amazon.com/boto3/latest/reference/services/appconfig/client/start_deployment.html#

3. Lambda function (python) uses urllib library to read configuration from AppConfig

https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-add.html

4. Lambda sends metrics to CloudWatch. Detects metrics on CloudWatch. Alarms active rollback

https://docs.aws.amazon.com/appconfig/latest/userguide/monitoring-overview.html

## Appconfig configuration profile
{
    'model_endpoint': model_endP,
    'model_adapter': ic_adapter,
    'template_uri': sysP_uri
}

## Operational logs sent to Cloudwatch as lambda log group
Latency at each stage

Error messages/completion status

request id

## Input/output log fields sent to S3:
endpoint_name

ic_adapter_name

prompt_template_uri (contains version)

input

output

##  Model drift logging sent to Cloudwatch under a model/adapter log group:
avg_rouge

min_rouge

max_rouge

count_entail

count_contradict

count_neutral
```
Python app
    │
    ├── PutMetricData
    ▼
CloudWatch Metrics
    ▼
CloudWatch Dashboard
```

