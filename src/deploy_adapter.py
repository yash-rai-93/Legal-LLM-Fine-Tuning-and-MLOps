import os
import sys
from datetime import datetime
from dotenv import load_dotenv
import mlrun

# Load environment variables into python variables
load_dotenv()  # will not throw an error if .env not present
ENV = os.environ["ENV"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
MLRUN_AWS_ROLE_ARN = os.environ["MLRUN_AWS_ROLE_ARN"]
HF_TOKEN = os.environ["HF_TOKEN"]

# MLRun setup =================================================


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
import src.utils as utils
import src.utils_model_registry as model_reg

# =======================================================


def deploy_new_adapter(
    context,
    # adapter_revision,
    model,
    model_tag,
    endpoint_name,
    base_ic_name,
    test_dataset,
    test_dataset_tag,
    prompt,
    prompt_tag,
    rolling_update: bool,
    eval_dataset=None,  # For future use when running eval during canary
    eval_dataset_tag=None,
):

    # ENV = os.environ["ENV"]
    # Check model tag status for deployment type
    champion_exists = model_reg.model_exists(
        project,
        "champion",
    )
    print(f"Champion status: {champion_exists}")

    if rolling_update:
        if not champion_exists:
            raise RuntimeError(
                "rolling_update=True, but a champion model does not exist"
            )
        initial_champion = model_reg.get_model_by_label(
            project,
            "champion",
        )
    elif not rolling_update:
        if champion_exists:
            raise RuntimeError("rolling_update=False, but a champion model exists")

    # Get the new model artifact
    new_model = model_reg.get_model_by_tag(
        project,
        model,
        model_tag,
    )

    # Define key
    key = datetime.now().strftime("%Y%m%d_%H%M")

    # Deploy LORA adapter as IC adapter
    adapter_revision = new_model.metrics["return"]["commit_oid:"]

    print("======> Beginning adapter deployment")
    adapter, adapter_name = utils.deploy_sm_lora_adapter(
        key,
        endpoint_name,
        base_ic_name,
        adapter_revision,
    )

    # adapter_name = "adapter-lmi-Hermes-FP8-2026-07-16-07-42-31-222"
    print(
        "Logs are under: /aws/sagemaker/InferenceComponents/base-lmi-Hermes-FP8-2026-xxxxxx"
    )

    # Run load tests on new model/adapter
    print("======> Beginning model pre-deployment validation")
    dataset_metrics, s3_eval_path = utils.process_multiple_row_testdata(
        project,
        endpoint_name,
        adapter_name,
        test_dataset,
        test_dataset_tag,
        prompt,
        prompt_tag,
        key,
    )

    check_count = dataset_metrics["count"] >= 1  # in real this would be 100
    check_accuracy = dataset_metrics["average_accuracy"] >= 0.7
    check_fmeasure = dataset_metrics["average_fmeasure"] >= 0.7

    result = check_count and check_accuracy and check_fmeasure
    if not result:
        raise Exception(
            f"model pre-deployment validation failed with values \n check_count: {dataset_metrics['count']}\n check_accuracy: {dataset_metrics['average_accuracy']}\n check_fmeasure {dataset_metrics['average_fmeasure']}"
        )
    print(
        f"✅ Endpoint passed model pre-deployment validation with metrics:\n {dataset_metrics}"
    )

    # Gradually shift traffic to new model by sending request to AppConfig with pre-configured deployment policy with rollback
    print(
        "Sending request to AppConfig to change variables in Lambda gateway while changing model tags"
    )

    try:
        if not rolling_update:
            # promote new model to champion
            model_reg.promote_champion(project, new_model)
        elif rolling_update:
            # Promote new model to challenger
            model_reg.promote_challenger(project, new_model)
            print(
                "✅ Deployment workflow finished, rolling update in progress, now monitoring new model in preparation for rollback"
            )
            utils.update_gateway_destination_sm(
                project,
                model_endpoint=endpoint_name,
                model_adapter=adapter_name,
                # This is hardcoded for development, didn't buy domain for mlrun
                template_uri="s3://sand-mlops-bucket-haviv/data/llm_prompt/contract_extractor_prompt/20260814_1626/contract_extractor_prompt.json",
                rolling=rolling_update,
                test_dataset=test_dataset,
                test_dataset_tag=test_dataset_tag,
            )
            # Demote old model to standby, promote new model to champion from challenger
            model_reg.demote_model(project, initial_champion)
            model_reg.promote_champion(project, new_model)
            model_reg.promote_challenger_demote_champion(
                project,
                initial_champion,
                new_model,
            )
            print(
                "✅ Rolling deployment finished, now monitoring new model in preparation for appconfig rollback for remaining duratino of bake time (if it exists)"
            )

    except Exception as e:
        # Return model tags to original state
        print("! Error. Returning models to initial states")
        model_reg.promote_champion(project, initial_champion)
        model_reg.demote_model(project, new_model)

        print(f"🚨 Deployment was unsuccessful: {e}")

    # Create a new long-term alarm for the deployment with (endpoint_name, adapter_name)
    utils.create_drift_alarm(endpoint_name, adapter_name, ENV)

    return {
        "endpoint_name": endpoint_name,
        "base_ic_name": base_ic_name,
        "adapter_name": adapter_name,
    }
