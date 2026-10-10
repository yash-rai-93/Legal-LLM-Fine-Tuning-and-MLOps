# This is what we want
from mlrun.artifacts.model import update_model

"""
Deploy model + adapter:
- linear: rejects if champion exists, promote to champion. Error: Undo champion
- rolling: challenger workflow. Error: Undo champion, repromote former-champion

Deploy adapter:
- linear: rejects if champion exists, promote to champion. Error: Undo champion
- rolling: challenger workflow. Error: Undo champion, repromote former-champion

Takedown:
- Manually delete
"""


# Get model artifact of model with champion/challenger tag (for error handling during deployment)
def get_model_by_label(project, status: str):
    """
    This is especially useful for canary deployment when a model challenger fails.
    """
    assert status in ("champion", "challenger")

    models = project.list_models(labels={"status": status})

    if models:
        model = models[-1]
        return model
    else:
        raise ValueError(f"Model with tag {status} does not exist")


def model_exists(project, status: str):

    assert status in ("champion", "challenger")

    models = project.list_models(labels={"status": status})

    if models:
        return True
    else:
        return False


def get_model_by_tag(
    project,
    model: str,
    tag: str,
):

    return project.get_artifact(key=model, tag=tag)


# Update operations
def promote_champion(
    project,
    model,
):
    models = project.list_models(labels={"status": "champion"})
    if not models:
        update_model(
            model_artifact=model, labels={"status": "champion"}, write_spec_copy=False
        )
    else:
        cha_tag = models[-1].to_dict()["metadata"]["tag"]
        raise KeyError(f"Champion model already exists: {cha_tag}")


def promote_challenger(
    project,
    model,
):
    """
    This is especially useful for canary deployment
    """
    chal_models = project.list_models(labels={"status": "challenger"})
    champ_models = project.list_models(labels={"status": "champion"})
    if champ_models:
        champion_tags = [x.to_dict()["metadata"]["tag"] for x in champ_models]
        challenger_model_tag = model.to_dict()["metadata"]["tag"]
        if challenger_model_tag in champion_tags:
            raise KeyError(
                f"Challenger model is a currently champion: {challenger_model_tag}"
            )
    if not chal_models:
        update_model(
            model_artifact=model, labels={"status": "challenger"}, write_spec_copy=False
        )
    else:
        chall_tag = chal_models[-1].to_dict()["metadata"]["tag"]
        raise KeyError(f"Challenger model already exists: {chall_tag}")


def demote_model(project, model):
    """
    This is especially useful for canary deployment when a model challenger fails.
    """

    update_model(
        model_artifact=model,
        labels={"status": "standby"},
        write_spec_copy=False,
    )


def promote_challenger_demote_champion(project, old_model, new_model):
    """
    This is used in canary deployment. Have to create challenger model first, for integrity check
    """

    demote_model(project, old_model)
    promote_champion(project, new_model)

    update_model(
        model_artifact=old_model,
        labels={"replaced": new_model.tag},
        write_spec_copy=False,
    )
