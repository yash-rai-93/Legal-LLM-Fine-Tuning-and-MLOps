#!/bin/bash

# Get the pre-signed download URL for that specific layer version
DOWNLOAD_URL=$(aws lambda get-layer-version-by-arn \
    --region us-east-1 \
    --arn arn:aws:lambda:us-east-1:027255383542:layer:AWS-AppConfig-Extension:328 \
    --query 'Content.Location' \
    --output text)

# Download and extract the layer into a local 'opt' directory
curl -o appconfig-ext.zip "$DOWNLOAD_URL"
mkdir -p opt
unzip appconfig-ext.zip -d opt/
rm appconfig-ext.zip