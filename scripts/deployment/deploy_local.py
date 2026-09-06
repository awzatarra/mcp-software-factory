"""Manual demo-local deployment. Standard library only; never invokes application tools."""

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from deployment_metadata import DeploymentError, Journal, atomic_write, deployment_lock, read_json, sha

REPOSITORY = "awzatarra/mcp-software-factory"
CONTROLLER_ROOT = Path(__file__).resolve().parents[2]
PROJECT = "mcp-software-factory"
VOLUMES = {"factory_workspace": "mcp-software-factory-workspace",
           "factory_data": "mcp-software-factory-data"}


def command(args, cwd, timeout=60):
    # Suppress subprocess text: Docker diagnostics can contain local configuration.
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
               "stderr": subprocess.PIPE, "shell": False, "cwd": cwd}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with subprocess.Popen(args, **options) as process:
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, shell=False, timeout=15)
            finally:
                process.kill()
                process.communicate()
            raise DeploymentError("deployment_command_timeout") from None
    if process.returncode:
        raise DeploymentError("deployment_command_failed")
    return stdout.decode("utf-8", errors="replace").strip()


def settings():
    if os.environ.get("CD_ENVIRONMENT") != "demo-local":
        raise DeploymentError("unsupported_environment")
    operation = os.environ.get("CD_OPERATION", "deploy")
    if operation not in ("deploy", "rollback"):
        raise DeploymentError("invalid_operation")
    candidate = sha(os.environ.get("CD_COMMIT_SHA"))
    raw_home = os.environ.get("SF_DEPLOY_HOME")
    if not raw_home or not Path(raw_home).is_absolute():
        raise DeploymentError("absolute_SF_DEPLOY_HOME_required")
    home = Path(raw_home).resolve()
    checkout = Path(os.environ.get("GITHUB_WORKSPACE", CONTROLLER_ROOT)).resolve()
    if home.is_relative_to(checkout) or home.is_relative_to(CONTROLLER_ROOT):
        raise DeploymentError("deployment_home_must_be_outside_checkout")
    return home, candidate, operation


def select_target(journal, candidate, operation):
    if operation == "rollback":
        previous = journal.data["previous_sha"]
        if not previous:
            raise DeploymentError("rollback_previous_sha_missing")
        if candidate != previous:
            raise DeploymentError("rollback_sha_mismatch")
    return candidate


def validate_source(source, candidate):
    if command(["git", "rev-parse", "HEAD"], source) != candidate:
        raise DeploymentError("checkout_sha_mismatch")
    remote = command(["git", "remote", "get-url", "origin"], source)
    if remote not in (f"https://github.com/{REPOSITORY}", f"https://github.com/{REPOSITORY}.git"):
        raise DeploymentError("unexpected_source_repository")
    command(["git", "merge-base", "--is-ancestor", candidate, "refs/remotes/origin/master"], source)
    if command(["git", "status", "--porcelain", "--untracked-files=all", "--ignored"], source):
        raise DeploymentError("source_checkout_not_clean")
    tracked = command(["git", "ls-files"], source).splitlines()
    for name in tracked:
        path = Path(name)
        if (path.name in (".env", ".env.production")
                or (name.startswith(("data/", "workspace/")) and path.name != ".gitkeep")):
            raise DeploymentError("source_contains_local_data")
    # Preserve the reviewed deployment architecture, even for historical candidates.
    if (source / "docker-compose.yml").read_bytes() != (CONTROLLER_ROOT / "docker-compose.yml").read_bytes():
        raise DeploymentError("candidate_compose_contract_changed")


def validate_evidence(home, candidate):
    evidence = read_json(home / "validated" / f"{candidate}.json")
    if (evidence.get("repository") != REPOSITORY or evidence.get("commit_sha") != candidate
            or evidence.get("status") != "passed"
            or evidence.get("kind") != "operator_platform_validation"
            or evidence.get("stores_compatible") is not True):
        raise DeploymentError("platform_validation_missing_or_mismatched")
    for key in ("validated_by", "evidence_reference"):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise DeploymentError("platform_validation_incomplete")
    checks = evidence.get("checks")
    if not isinstance(checks, list) or not checks or not all(
        isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()
        and c.get("result") == "passed" for c in checks
    ):
        raise DeploymentError("platform_validation_checks_missing")
    try:
        validated = datetime.fromisoformat(evidence["validated_at"])
        if validated.tzinfo is None or validated > datetime.now(timezone.utc):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise DeploymentError("platform_validation_date_invalid") from None


def validate_window(home, candidate):
    window = read_json(home / "maintenance.json")
    try:
        expires = datetime.fromisoformat(window["expires_at"])
        valid = (window["commit_sha"] == candidate and window["quiescent"] is True
                 and expires.tzinfo is not None and expires > datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise DeploymentError("maintenance_window_not_confirmed")


def compose_config(source, home, candidate):
    env_file = home / "config" / ".env.production"
    if not env_file.is_file():
        raise DeploymentError("external_backend_configuration_missing")
    config = json.loads(command([
        "docker", "compose", "--env-file", str(env_file), "-f", str(source / "docker-compose.yml"),
        "config", "--format", "json", "--no-env-resolution",
    ], source))
    if set(config["services"]) != {"backend", "frontend"}:
        raise DeploymentError("unexpected_compose_services")
    for key, expected in VOLUMES.items():
        if config["volumes"][key]["name"] != expected:
            raise DeploymentError("persistent_volume_mismatch")
    for service, port, target in (("backend", 8000, 8000), ("frontend", 5173, 80)):
        item = config["services"][service]
        ports = item.get("ports", [])
        if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1" or (
            str(ports[0]["published"]) != str(port) or ports[0]["target"] != target
        ):
            raise DeploymentError("non_loopback_compose_port")
        item["image"] = f"mcp-software-factory-{service}:{candidate}"
        item["build"].setdefault("labels", {})["org.opencontainers.image.revision"] = candidate
    config["services"]["backend"]["env_file"] = [{"path": str(env_file), "required": True}]
    config["name"] = PROJECT
    return config


class Docker:
    def __init__(self, source, compose_file):
        self.source = source
        self.prefix = ["docker", "compose", "-p", PROJECT, "-f", str(compose_file)]

    def compose(self, *args, timeout=60):
        return command([*self.prefix, *args], self.source, timeout)

    def inspect(self, *args):
        return json.loads(command(["docker", "inspect", *args], self.source))

    def preflight(self):
        context = json.loads(command(["docker", "context", "inspect"], self.source))[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
        if os.environ.get("DOCKER_HOST") or not endpoint.startswith(("npipe://", "unix://")):
            raise DeploymentError("local_docker_engine_required")
        if command(["docker", "info", "--format", "{{.OSType}}"], self.source) != "linux":
            raise DeploymentError("linux_containers_required")
        # Refuse unrelated listeners before starting a build or replacing services.
        for service, port in (("backend", 8000), ("frontend", 5173)):
            with socket.socket() as sock:
                sock.settimeout(1)
                occupied = sock.connect_ex(("127.0.0.1", port)) == 0
            if occupied:
                ids = self.compose("ps", "--all", "--quiet", service).splitlines()
                if len(ids) != 1:
                    raise DeploymentError("deployment_port_conflict")
                binding = self.inspect(ids[0])[0]["NetworkSettings"]["Ports"]
                if not any(b.get("HostIp") == "127.0.0.1" and b.get("HostPort") == str(port)
                           for bindings in binding.values() for b in (bindings or [])):
                    raise DeploymentError("deployment_port_conflict")

    def image_ids(self, candidate):
        images = {}
        for service in ("backend", "frontend"):
            image = self.inspect(f"mcp-software-factory-{service}:{candidate}")[0]
            if image["Config"]["Labels"].get("org.opencontainers.image.revision") != candidate:
                raise DeploymentError("image_sha_mismatch")
            images[service] = image["Id"]
        return images

    def verify_running(self, images):
        for service, expected in images.items():
            ids = self.compose("ps", "--quiet", service).splitlines()
            if len(ids) != 1:
                raise DeploymentError("deployment_container_missing")
            item = self.inspect(ids[0])[0]
            if (item["Image"] != expected or not item["State"]["Running"]
                    or item["Config"]["Labels"].get("com.docker.compose.project") != PROJECT):
                raise DeploymentError("running_image_mismatch")


def health_check(timeout=120, clock=time.monotonic, sleep=time.sleep, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            for url, backend in (("http://127.0.0.1:8000/health", True),
                                 ("http://127.0.0.1:5173/", False)):
                remaining = deadline - clock()
                if remaining <= 0:
                    raise DeploymentError("health_timeout")
                with opener(url, timeout=min(5, remaining)) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise ValueError
                    if backend and json.loads(response.read(4096)).get("status") != "ok":
                        raise ValueError
            if clock() <= deadline:
                return
        except (OSError, ValueError):
            pass
        sleep(max(0, min(5, deadline - clock())))
    raise DeploymentError("health_timeout")


def deploy(home, source, candidate, operation, attempt_id, docker_factory=Docker, health=health_check):
    with deployment_lock(home):
        journal = Journal(home)
        # Do not re-execute an already successful attempt, including rollback.
        attempt = journal.begin(attempt_id, candidate, operation)
        if attempt is None:
            print("Deployment attempt already completed; no side effects repeated.")
            return
        stage = "validate"
        try:
            select_target(journal, candidate, operation)
            validate_source(source, candidate)
            validate_evidence(home, candidate)
            validate_window(home, candidate)
            with tempfile.TemporaryDirectory(prefix="compose-", dir=home) as temporary:
                config_file = Path(temporary) / "compose.json"
                atomic_write(config_file, compose_config(source, home, candidate))
                docker = docker_factory(source, config_file)
                docker.preflight()
                stage = "build"
                print(f"Build environment=demo-local sha={candidate}")
                # Healthy images already recorded for this SHA are kept for reproducible rollback.
                recorded = next((a for a in reversed(journal.data["attempts"])
                                 if a["deployed_sha"] == candidate and a["status"] in ("healthy", "rolled_back")), None)
                if recorded:
                    images = docker.image_ids(candidate)
                    if images != recorded.get("image_ids"):
                        raise DeploymentError("retained_images_missing_or_changed")
                else:
                    docker.compose("build", timeout=1800)
                    images = docker.image_ids(candidate)
                attempt["image_ids"] = images
                validate_window(home, candidate)
                journal.deploying(attempt)
                stage = "deploy"
                docker.compose("up", "-d", "--no-build", "--pull", "never", timeout=180)
                stage = "health"
                docker.verify_running(images)
                health()
                docker.verify_running(images)
            stage = "metadata"
            journal.finish(attempt, True)
            print(json.dumps({k: attempt[k] for k in (
                "environment", "deployed_sha", "version", "deployed_at", "status", "current_sha", "previous_sha", "attempt_id"
            )}))
        except Exception:
            # Reload after write failures so an in-memory success cannot mask a failed save.
            persisted = Journal(home)
            pending = next(a for a in persisted.data["attempts"] if a["attempt_id"] == attempt_id)
            persisted.finish(pending, False, f"{stage}_failed")
            raise DeploymentError(f"{stage}_failed") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("select", "run"))
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    try:
        home, candidate, operation = settings()
        if args.action == "select":
            select_target(Journal(home), candidate, operation)
            output = os.environ.get("GITHUB_OUTPUT")
            if output:
                with open(output, "a", encoding="utf-8") as stream:
                    stream.write(f"sha={candidate}\n")
            print(f"Selected SHA: {candidate}")
        else:
            if args.source is None:
                raise DeploymentError("source_checkout_required")
            source = args.source.resolve()
            if home.is_relative_to(source):
                raise DeploymentError("deployment_home_must_be_outside_checkout")
            deploy(home, source, candidate, operation, os.environ.get("CD_ATTEMPT_ID", ""))
    except Exception as error:
        code = str(error) if isinstance(error, DeploymentError) else "deployment_failed"
        print(f"Deployment failed: {code}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
