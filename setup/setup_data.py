"""
Run this file from main directory to set up data in the project environment
"""

# from dotenv import load_dotenv
# import os
# import boto3

# load_dotenv()
# env_name = os.environ["ENV"]

# LOCAL_FOLDER = "./dataset_engineering/processed_parquet"
# BUCKET_NAME = f"{env_name}-mlops-bucket-haviv"
# S3_PREFIX = "data/raw"

# s3 = boto3.client(
#     "s3",
#     region_name="us-east-1",
#     endpoint_url="https://s3.amazonaws.com",
# )

# for root, dirs, files in os.walk(LOCAL_FOLDER):
#     for filename in files:
#         local_path = os.path.join(root, filename)

#         # Preserve the local folder structure in S3
#         relative_path = os.path.relpath(local_path, LOCAL_FOLDER)
#         s3_key = os.path.join(S3_PREFIX, relative_path).replace("\\", "/")

#         print(f"Uploading: {local_path} -> s3://{BUCKET_NAME}/{s3_key}")

#         s3.upload_file(
#             local_path,
#             BUCKET_NAME,
#             s3_key,
#         )
