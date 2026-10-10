"""
A LARGE PART OF THIS BUILD SCRIPT HAS BEEN COMMENTED OUT BECAUSE EACH FUNCTION BUILD TAKES 5-10 MINUTES CAUSING THE BUILD TO TAKE UP TO 45 MINUTES

-Builds MLRun function images
-This file is run after the MLRun project has been created, and the function code has been committed to github.
-Run this file from main directory
-This file is only meant to run in CI/CD environment. Variables should be loaded before hand in a the job or in an .env file (local test)
"""

import mlrun
import os
import sys

# BRANCH = os.environ["BRANCH"]
ENV = os.environ["ENV"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
MLRUN_AWS_ROLE_ARN = os.environ["MLRUN_AWS_ROLE_ARN"]
HF_TOKEN = os.environ["HF_TOKEN"]
# in CI/CD this will be a unique variable from the github actions run. local dev uses "latest"
IMAGE_TAG = os.environ["IMAGE_TAG"]
FEATURE_BRANCH = os.environ["FEATURE_BRANCH"]

if os.environ.get("MLRUN_DBPATH"):
    print("Detected K8s environment")
    project = mlrun.load_project(
        name="legalcontractextractor", context="/home/mlrun_code/"
    )
else:
    print("Detected Local environment")
    # ====== If run from notebooks, the working directory is /notebooks =====
    parent_dir = os.path.abspath("..")
    sys.path.append(parent_dir)
    # ====== This is necessary for importing other files from src when running locally =====
    mlrun.set_environment(api_path="http://localhost:30070")
    # Context must be where project.yaml is, if running from notebook use ../
    project = mlrun.load_project(name="legalcontractextractor", context="../")
    print("Successfully loaded project")

# Configuration for the build
# Source branch for build
url = f"git://github.com/jerrold110/Finetune-legal-llm-mlops.git#refs/heads/{FEATURE_BRANCH}"
project.set_source(
    source=url,
    pull_at_runtime=False,
)
print("Set project source")
# for projects that require cloning the entire repo (clones project.yaml too)
# project.set_secrets(secrets={"GIT_TOKEN" : "XXXXXXXXXXXXXXX"}, provider="kubernetes") private repo

# =======================================================
# Register datasets
# =======================================================
raw_proc_fn = project.set_function(
    name="build-raw-proc",
    func="src/register_raw_dataset.py",
    handler="process_raw",
    image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    kind="job",
    with_repo=False,  # This can be false, single code file, no need to copy whole repo and no does load_project() which requires project.yaml file
)

raw_proc_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
raw_proc_fn.set_env_from_secret(
    "AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID"
)
raw_proc_fn.set_env_from_secret(
    "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
)
raw_proc_fn.set_env("AWS_ENDPOINT_URL_S3", "https://s3.amazonaws.com")
raw_proc_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
raw_proc_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
raw_proc_fn.set_env(name="ENV", value=ENV)
raw_proc_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
raw_proc_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

raw_proc_name = "raw-proc"
raw_proc_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{raw_proc_name}:{IMAGE_TAG}"

project.build_function(
    base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    function=raw_proc_fn,
    image=raw_proc_image_name,
    force_build=True,
    with_mlrun=False,  # This is very important, or else it changes the base image
    secret_name="ecr-pull-secret",
)

# =======================================================
# No Train (ENTRE REPO)
# =======================================================
eval_nt_fn = project.set_function(
    name="build-eval-notrain",
    func="src/evaluate_no_train.py",
    handler="evaluate_model",
    image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    kind="job",
    # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
    with_repo=True,
)

eval_nt_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
eval_nt_fn.set_env_from_secret(
    "AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID"
)
eval_nt_fn.set_env_from_secret(
    "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
)
eval_nt_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
eval_nt_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
eval_nt_fn.set_env(name="ENV", value=ENV)
eval_nt_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
eval_nt_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

eval_nt_name = "eval-notrain"
eval_nt_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{eval_nt_name}:{IMAGE_TAG}"

print("Pulling Repo from GitHub and building...")

project.build_function(
    base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    function=eval_nt_fn,
    image=eval_nt_image_name,
    with_mlrun=False,  # This is very important, or else it changes the base image
    force_build=True,
    secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
)

# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# Test function for CI/cD
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

_check_fn = project.set_function(
    name="build-check",
    func="src/_check.py",
    handler="checkfoo",
    image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    kind="job",
    # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
    with_repo=True,
)

_check_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
_check_fn.set_env_from_secret(
    "AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID"
)
_check_fn.set_env_from_secret(
    "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
)
_check_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
_check_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
_check_fn.set_env(name="ENV", value=ENV)
_check_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
_check_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

_check_name = "check"
_check_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{_check_name}:{IMAGE_TAG}"

print("Pulling Repo from GitHub and building...")

project.build_function(
    base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
    function=_check_fn,
    image=_check_image_name,
    with_mlrun=False,  # This is very important, or else it changes the base image
    force_build=True,
    secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
)

# # =======================================================
# # Train (ENTRE REPO)
# # =======================================================
# eval_t_fn = project.set_function(
#     name="build-eval-train",
#     func="src/evaluate_train.py",
#     handler="evaluate_model_train",
#     image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     kind="job",
#     # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
#     with_repo=True,
# )

# eval_t_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
# eval_t_fn.set_env_from_secret("AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID")
# eval_t_fn.set_env_from_secret(
#     "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
# )
# eval_t_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
# eval_t_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
# eval_t_fn.set_env(name="ENV", value=ENV)
# eval_t_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
# eval_t_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

# eval_t_name = "eval-train"
# eval_t_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{eval_t_name}:{IMAGE_TAG}"

# print("Pulling Repo from GitHub and building...")

# project.build_function(
#     base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     function=eval_t_fn,
#     image=eval_t_image_name,
#     with_mlrun=False,  # This is very important, or else it changes the base image
#     force_build=True,
#     secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
# )

# # =======================================================
# # Register model
# # =======================================================
# reg_mod_fn = project.set_function(
#     name="build-register-model",
#     func="src/register_model.py",
#     handler="register_new_model",
#     image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     kind="job",
#     # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
#     with_repo=True,
# )

# reg_mod_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
# reg_mod_fn.set_env_from_secret("AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID")
# reg_mod_fn.set_env_from_secret(
#     "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
# )
# reg_mod_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
# reg_mod_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
# reg_mod_fn.set_env(name="ENV", value=ENV)
# reg_mod_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
# reg_mod_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

# reg_mod_name = "register-model"
# reg_mod_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{reg_mod_name}:{IMAGE_TAG}"

# print("Pulling Repo from GitHub and building...")

# project.build_function(
#     base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     function=reg_mod_fn,
#     image=reg_mod_image_name,
#     with_mlrun=False,  # This is very important, or else it changes the base image
#     force_build=True,
#     secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
# )

# # =======================================================
# # Deploy model + adapter
# # =======================================================
# dep_mod_adapt_fn = project.set_function(
#     name="build-deploy-model-adapter",
#     func="src/deploy_model.py",
#     handler="deploy_new_model_adapter",
#     image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     kind="job",
#     # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
#     with_repo=True,
# )

# dep_mod_adapt_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
# dep_mod_adapt_fn.set_env_from_secret("AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID")
# dep_mod_adapt_fn.set_env_from_secret(
#     "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
# )
# dep_mod_adapt_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
# dep_mod_adapt_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
# dep_mod_adapt_fn.set_env(name="ENV", value=ENV)
# dep_mod_adapt_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
# dep_mod_adapt_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

# dep_mod_adapt_name = "deploy-model-adapter"
# dep_mod_adapt_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{dep_mod_adapt_name}:{IMAGE_TAG}"

# print("Pulling Repo from GitHub and building...")

# project.build_function(
#     base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     function=dep_mod_adapt_fn,
#     image=dep_mod_adapt_image_name,
#     with_mlrun=False,  # This is very important, or else it changes the base image
#     force_build=True,
#     secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
# )

# # =======================================================
# # Deploy adapter
# # =======================================================
# dep_adapt_fn = project.set_function(
#     name="build-deploy-adapter-existing-model",
#     func="src/deploy_adapter.py",
#     handler="deploy_new_adapter",
#     image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     kind="job",
#     # THIS CLONES THE ENTIRE REPO, FOR MULTI SOURCE FILES
#     with_repo=True,
# )

# dep_adapt_fn.set_image_pull_configuration(image_pull_secret_name="ecr-pull-secret")
# dep_adapt_fn.set_env_from_secret("AWS_ACCESS_KEY_ID", "aws-creds-literal", "AWS_ACCESS_KEY_ID")
# dep_adapt_fn.set_env_from_secret(
#     "AWS_SECRET_ACCESS_KEY", "aws-creds-literal", "AWS_SECRET_ACCESS_KEY"
# )
# dep_adapt_fn.set_env(name="MLRUN_AWS_ROLE_ARN", value=MLRUN_AWS_ROLE_ARN)
# dep_adapt_fn.set_env(name="HF_TOKEN", value=HF_TOKEN)
# dep_adapt_fn.set_env(name="ENV", value=ENV)
# dep_adapt_fn.set_env(name="ACCOUNT_ID", value=ACCOUNT_ID)
# dep_adapt_fn.set_env(name="AWS_ENDPOINT_URL_S3", value="https://s3.amazonaws.com")

# dep_adapt_name = "deploy-adapter"
# dep_adapt_image_name = f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/function-{dep_adapt_name}:{IMAGE_TAG}"

# print("Pulling Repo from GitHub and building...")

# project.build_function(
#     base_image=f"{ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/{ENV}/mlrun-myjob:{IMAGE_TAG}",
#     function=dep_adapt_fn,
#     image=dep_adapt_image_name,
#     with_mlrun=False,  # This is very important, or else it changes the base image
#     force_build=True,
#     secret_name="ecr-pull-secret",  # This argument may be not working in 1.11.0 ???
# )
