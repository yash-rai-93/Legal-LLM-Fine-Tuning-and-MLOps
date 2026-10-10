"""
This reads from the MLRun metadata database

No environment variables needed
"""

import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables into python variables
load_dotenv()  # will not throw an error if .env not present
ENV = os.environ["ENV"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
MLRUN_AWS_ROLE_ARN = os.environ["MLRUN_AWS_ROLE_ARN"]
HF_TOKEN = os.environ["HF_TOKEN"]

# MLRun setup =================================================
import mlrun

"""
Set the environment for execution:
Running inside the cluster - MLRun already knows the right address from environment variable
https://docs.mlrun.org/en/1.11.x/setup-guide.html

Running locally, use the mlrun-api service NodePort
kubectl --namespace mlrun get svc | grep -i api
"""

# print("Debug. variables for confirmation")
# print("cwd:", os.getcwd())
# print("__file__ dir:", os.path.dirname(os.path.abspath(__file__)))
# print("contents:", os.listdir("."))
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
# import other utils files

# =======================================================


def register_new_model(
    context,
    experiment_run_uid,
    model_key,
    project_name,
):
    # model-purpose-artifacts
    # model_key =
    # Define version
    version = datetime.now().strftime("%Y%m%d_%H%M")

    # Initialize the MLRun DB client
    db = mlrun.get_run_db()
    run_dict = db.read_run(uid=experiment_run_uid, project="legalcontractextractor")

    # Convert the dictionary to a RunObject for easier API access
    # run = RunObject.from_dict(run_dict)
    # output = run.outputs['return'] # this is what was returned
    try:
        run_parameters = run_dict["spec"]["parameters"]
        run_metrics = run_dict["status"]["results"]
        output = run_dict["status"]["results"]["return"]
        oid = output["commit_oid:"]  # bug here
    except KeyError as e:
        raise RuntimeError(
            f"Required parameter from run is missing: {experiment_run_uid}\n{e}"
        ) from e

    # Ensure model_uri does not already exist (so no duplicates)
    artifacts = db.list_artifacts(project=project_name, name=model_key, kind="model")

    existing_urls = [x["spec"]["model_url"] for x in artifacts]
    assert (
        oid not in existing_urls
    ), f"Model with model_url {oid} already exists. Creating this again will create a duplicate model pointing to the same object"

    # Pass in model_id, commit, hyperparameters, performance metrics
    # version = datetime.now().strftime("%Y%m%d_%H%M")

    model = project.log_model(
        key=f"{model_key}",
        tag=version,  # Unique
        labels={"status": "standby", "type": "Hermes-4 14B adapter"},
        metrics=run_metrics,
        parameters=run_parameters,
        model_url=oid,
        upload=False,
    )
    print("========== MODEL METADATA ==========")
    print(model.tag)
    print(model.labels)
    print(model.model_url)
    print(model.metrics)
    print(model.parameters)

    print(f"{model_key} model logged with version:\n {version}")
