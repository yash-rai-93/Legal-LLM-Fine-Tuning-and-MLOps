def checkfoo(
    context,
    key,
    tag
):
    """
    This is a function used for smoke tests used in lieu of a unit/integration/system tests.
    It tests basic functionality as MLOps pipeline take a long time to run fully.
    """

    print("Hello from check.foo function")

    # Test 1
    import mlrun
    import os

    if os.environ.get("MLRUN_DBPATH"):
        print("Detected K8s environment")
        project = mlrun.load_project(
            name="legalcontractextractor", context="/home/mlrun_code/"
        )
    else:
        raise SystemError("❌ MLRUN_DBPATH environment variable is missing")
    print("✅ Test 1 passed project loaded")

    # Test 2
    import torch
    import sagemaker
    from transformers import AutoTokenizer
    import pyarrow.dataset as ds

    print("✅ Test 2 passed dependencies working")

    # Test 3
    import boto3

    sts_client = boto3.client("sts")
    identity = sts_client.get_caller_identity()
    print("✅ Test 3 passed AWS Authentication successful!")

    # Test 4
    import src.utils_model_registry
    import src.utils
    import src.utils_evaluate_model

    print("✅ Test 4 passed. Other source code files present")

    # Test 5
    project.log_model(
        key=key,
        tag=tag,
        metrics={"key": "value"},
        parameters={"accuracy": 100.0},
        model_url="www.myurl.com",
        upload=False,
    )

    model = project.get_artifact(
        key=key,
        tag=tag,
    )
    model_d = model.to_dict()

    assert model is not None
    assert model_d, "❌ The dictionary is empty!"

    print("✅ Test 5 passed. Model object created and re-loaded")
