"""
These tests areintended to be run in the CICD pipeline. They test all the basic functionality of the MLRun pipelines without utilising any expensive cloud resoures (Sagemaker) to save cost and a significant amount of time.

This function covers:
- Able to load project in job environment
- Dependencies are installed
- AWS authentication
- Source code files are copied into the image from GitHub
- A model artifact can be created on the model registry and loaded

"""

import mlrun

print(f"mlrun import in the runner file: {__file__} successful")


def test_cicd_basic():
    """
    This runs in the CICD pipeline. Outside the kubernetes cluster.

    MLRun 1.11.0 and python 3.11 should be installed
    """

    mlrun.set_environment(api_path="http://localhost:30070")
    # Where is the working directory during CI/CD ?
    mlrun.load_project(
        name="legalcontractextractor",
        context="./",
    )

    mlrun.run_function(
        "build-check",
        params={"key": "test_model_artifact", "tag": "0.0.0"},
        local=False,
    )
