# Run this from main wd
from pathlib import Path
import mlrun

# check is if project.yaml exists in cwd
file_path = Path("./project.yaml")
if file_path.is_file():
    print("A project file already exists.")
    raise SystemExit("project.yaml exists in this location")


mlrun.set_environment(api_path="http://localhost:30070")

project = mlrun.new_project(
    name="legalcontractextractor",
    user_project=False,
    init_git=False,
    description="MLOps system for legal contractor extractor LLM",
    overwrite=False,
)
print(project.spec.get_code_path())
