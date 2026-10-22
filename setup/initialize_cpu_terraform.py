"""Install and initialize the pinned Terraform toolchain for a private CPU scaffold.

Administrative CloudShell helper only. Runs Terraform version/init; never plan,
import or apply. Installs under /tmp and retains the provider lock privately.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

TERRAFORM_VERSION = "1.16.6"
AWS_PROVIDER_VERSION = "6.67.0"
RELEASE_BASE = "https://releases.hashicorp.com/terraform/" + TERRAFORM_VERSION + "/"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def download(name, destination, limit):
    with urllib.request.urlopen(RELEASE_BASE + name, timeout=60) as response:
        require(response.geturl().startswith(RELEASE_BASE), "Unexpected release redirect")
        total = 0
        with destination.open("xb") as output:
            while True:
                part = response.read(1024 * 1024)
                if not part:
                    break
                total += len(part)
                require(total <= limit, "Release download exceeds expected bounds")
                output.write(part)
    return destination.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-dir", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    workspace = Path(args.workspace_dir).expanduser().resolve()
    private_root = (Path.home() / "legal-llm-deploy/infrastructure").resolve()
    require(workspace.is_relative_to(private_root) and workspace.parent.name == "terraform-planning",
            "Use the private prepared CPU planning workspace")
    preparation = json.loads((workspace / "preparation-summary.json").read_text())
    require(preparation["Preparation"] == "READY"
            and preparation["VerifiedLambdaPackages"] == 4
            and Path(preparation["WorkspaceDirectory"]).resolve() == workspace,
            "Require a completed import preparation")
    require(not (workspace / "toolchain.json").exists(), "Toolchain already recorded; inspect it before rerunning")
    require(not any(workspace.glob("*.tfstate*")), "Workspace already contains Terraform state")
    require({p.name for p in workspace.glob("*.tf")} == {"imports.tf"}
            and {p.name for p in workspace.glob("*.tf.json")} == {"versions.tf.json"},
            "Require the untouched planning scaffold")
    configuration = json.loads((workspace / "versions.tf.json").read_text())
    terraform = configuration["terraform"]
    require(terraform["required_version"] == "= " + TERRAFORM_VERSION
            and terraform["required_providers"] == {
                "aws": {"source": "hashicorp/aws", "version": "= " + AWS_PROVIDER_VERSION}}
            and terraform["backend"] == {"local": {"path": str(workspace / "terraform.tfstate")}},
            "Unexpected toolchain or backend configuration")
    require(platform.system() == "Linux", "Run this helper in AWS CloudShell")
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    require(architecture is not None, "Unsupported CloudShell architecture")
    require(shutil.disk_usage("/tmp").free >= 2 * 1024 ** 3,
            "Require at least 2 GiB free temporary disk for provider downloads")
    runtime = Path(tempfile.mkdtemp(prefix="legal-llm-cpu-terraform-", dir="/tmp"))
    runtime.chmod(0o700)
    archive_name = "terraform_" + TERRAFORM_VERSION + "_linux_" + architecture + ".zip"
    print("Downloading the pinned Terraform release to temporary storage.", flush=True)
    sums = download("terraform_" + TERRAFORM_VERSION + "_SHA256SUMS",
                    runtime / "SHA256SUMS", 1024 * 1024).decode("utf-8")
    expected = [line.split()[0] for line in sums.splitlines()
                if len(line.split()) == 2 and line.split()[1].lstrip("*") == archive_name]
    require(len(expected) == 1 and re.fullmatch(r"[0-9a-f]{64}", expected[0]),
            "Missing or ambiguous official release checksum")
    archive = runtime / archive_name
    payload = download(archive_name, archive, 150 * 1024 * 1024)
    require(hashlib.sha256(payload).hexdigest() == expected[0], "Terraform release checksum mismatch")
    with zipfile.ZipFile(archive) as zipped:
        entry = zipped.getinfo("terraform")
        require(0 < entry.file_size <= 400 * 1024 * 1024, "Unexpected Terraform binary size")
        executable = runtime / "terraform"
        with zipped.open(entry) as src, executable.open("xb") as output:
            shutil.copyfileobj(src, output)
    executable.chmod(0o700)
    cli_config = runtime / "terraform.rc"
    cli_config.write_text("disable_checkpoint = true\n", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("TF_") and key != "TERRAFORM_CONFIG"}
    environment.update({
        "TF_DATA_DIR": str(runtime / "data"), "TF_CLI_CONFIG_FILE": str(cli_config),
        "TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1",
    })
    version = subprocess.run([str(executable), "version", "-json"], env=environment,
                             capture_output=True, text=True, timeout=30, check=True)
    require(json.loads(version.stdout)["terraform_version"] == TERRAFORM_VERSION,
            "Installed Terraform version mismatch")
    metadata = {
        "Status": "INITIALIZING", "TerraformVersion": TERRAFORM_VERSION,
        "AWSProviderVersion": AWS_PROVIDER_VERSION, "ArchiveSha256": expected[0],
        "Executable": str(executable), "DataDirectory": environment["TF_DATA_DIR"],
        "CLIConfiguration": str(cli_config), "WorkspaceDirectory": str(workspace),
        "LogFile": str(workspace / "terraform-init.log"),
        "TerraformPlanStarted": False, "AWSChangesMade": False,
        "TerraformStateChanged": False,
    }
    write_json(workspace / "toolchain.json", metadata)
    print("Initializing the local planning backend and pinned AWS provider; this may take a few minutes.", flush=True)
    with (workspace / "terraform-init.log").open("w", encoding="utf-8") as log:
        try:
            result = subprocess.run(
                [str(executable), "init", "-input=false", "-no-color"],
                cwd=workspace, env=environment, stdout=log, stderr=subprocess.STDOUT,
                timeout=600, check=False)
        except subprocess.TimeoutExpired:
            metadata["Status"] = "INIT_TIMEOUT"
            write_json(workspace / "toolchain.json", metadata)
            raise ValueError("Initialization timed out; inspect the private init log")
    metadata["Status"] = "READY" if result.returncode == 0 else "INIT_FAILED"
    metadata["InitExitCode"] = result.returncode
    metadata["ProviderLockCreated"] = (workspace / ".terraform.lock.hcl").is_file()
    metadata["TerraformStateChanged"] = any(workspace.glob("*.tfstate*"))
    write_json(workspace / "toolchain.json", metadata)
    require(result.returncode == 0, "Initialization failed; inspect the private init log")
    require(metadata["ProviderLockCreated"] and not metadata["TerraformStateChanged"],
            "Unexpected initialization result; inspect private workspace")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(json.dumps({"Toolchain": "STOPPED", "Error": type(exc).__name__,
                          "Reason": str(exc) if type(exc) is ValueError else "Inspect the private workspace and init log",
                          "TerraformPlanStarted": False, "AWSChangesMade": False}))
        raise SystemExit(1)
