# Official AWS Lambda base image for Python 3.11
# https://docs.aws.amazon.com/lambda/latest/dg/python-image.html
# https://github.com/aws/aws-lambda-base-images/blob/python3.11/Dockerfile.python3.11

# https://docs.aws.amazon.com/prescriptive-guidance/latest/patterns/deploy-lambda-functions-with-container-images.html#deploy-lambda-functions-with-container-images-additional
FROM public.ecr.aws/lambda/python:3.11

COPY opt/ /opt/

# Lambdas working directory is at the variable LAMBDA_TASK_ROOT -> /var/task
COPY requirements_lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install -r ${LAMBDA_TASK_ROOT}/requirements_lambda.txt

COPY model_gateway.py ${LAMBDA_TASK_ROOT}/

# Environment variables for polling appconfig
# https://docs.aws.amazon.com/appconfig/latest/userguide/appconfig-integration-lambda-extensions-config.html
ENV AWS_APPCONFIG_EXTENSION_POLL_INTERVAL_SECONDS="10"
ENV AWS_APPCONFIG_EXTENSION_POLL_TIMEOUT_MILLIS="3000"

ARG ENV
ENV ENV=${ENV}

# Set the function to run
CMD ["model_gateway.lambda_handler"]