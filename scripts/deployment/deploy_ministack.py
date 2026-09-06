"""Manual MiniStack ECR + local Compose deployment; no ECS or real AWS."""

import argparse
import json
import os
import socket
import tempfile
import time
from pathlib import Path

import deploy_local as local
from deployment_metadata import DeploymentError, Journal, atomic_write, deployment_lock, sha
from ministack_ecr import ECR, ENDPOINT, REGISTRY, REPOSITORIES, image_reference, isolated_environment, local_opener, validate_registry_host

TARGET = "demo-aws-emulated"
PROJECT = "sf-demo-aws"
MINISTACK = "sf-demo-aws-ministack"
MINISTACK_IMAGE = "ministackorg/ministack@sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726"
LABEL = "sf.deployment.target"
ROOT = Path(__file__).resolve().parents[2]
COMPOSE = "docker-compose.ministack.yml"
PORTS = {"backend": (18000, "8000/tcp"), "frontend": (15173, "80/tcp")}
VOLUMES = ("sf-demo-aws-workspace", "sf-demo-aws-data", "sf-demo-aws-ministack-state")


def settings(require_sha=True):
    if os.environ.get("CD_ENVIRONMENT") != TARGET:
        raise DeploymentError("unsupported_environment")
    raw = os.environ.get("SF_MINISTACK_DEPLOY_HOME")
    if not raw or not Path(raw).is_absolute():
        raise DeploymentError("absolute_SF_MINISTACK_DEPLOY_HOME_required")
    home = Path(raw).resolve()
    roots = [ROOT, Path(os.environ.get("GITHUB_WORKSPACE", ROOT)).resolve(),
             Path(r"C:\software-factory-deploy").resolve()]
    if os.environ.get("SF_DEPLOY_HOME"):
        roots.append(Path(os.environ["SF_DEPLOY_HOME"]).resolve())
    if any(home.is_relative_to(root) or root.is_relative_to(home) for root in roots):
        raise DeploymentError("deployment_home_must_be_separate")
    operation = os.environ.get("CD_OPERATION", "deploy")
    if operation not in ("deploy", "rollback"):
        raise DeploymentError("invalid_operation")
    return home, sha(os.environ.get("CD_COMMIT_SHA")) if require_sha else None, operation


def check_bindings(item, expected):
    ports = item["NetworkSettings"].get("Ports") or {}
    published = {key: values for key, values in ports.items() if values}
    if set(published) != set(expected):
        raise DeploymentError("deployment_binding_mismatch")
    for key, host_port in expected.items():
        if len(published[key]) != 1 or published[key][0] != {"HostIp": "127.0.0.1", "HostPort": str(host_port)}:
            raise DeploymentError("non_loopback_container_binding")


def health(urls, timeout=120, opener=None):
    opener = opener or local_opener()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            for url, backend in urls:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                with opener.open(url, timeout=min(5, remaining)) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise ValueError
                    if backend and json.loads(response.read(4096)).get("status") != "ok":
                        raise ValueError
            else:
                if time.monotonic() <= end:
                    return
        except (OSError, ValueError):
            pass
        time.sleep(max(0, min(2, end - time.monotonic())))
    raise DeploymentError("health_timeout")


class Runtime:
    def __init__(self, source, home):
        self.source, self.home = source, home
        self.config = None
        self.env = isolated_environment()

    def run(self, *args, operation=None, timeout=60, extra=None):
        return local.command(list(args), self.source, timeout, operation,
                             env={**self.env, **(extra or {})})

    def inspect(self, identifier, kind="container"):
        return json.loads(self.run("docker", kind, "inspect", identifier, operation="docker_inspect"))[0]

    def ids(self):
        return self.run("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + PROJECT).split()

    def registry_id(self):
        ids = self.run("docker", "ps", "-aq", "--filter", "name=^/" + MINISTACK + "$" ).split()
        if len(ids) > 1:
            raise DeploymentError("ministack_identity_conflict")
        return ids[0] if ids else None

    def owned_registry(self, identifier):
        item = self.inspect(identifier)
        if item["Config"].get("Labels", {}).get(LABEL) != TARGET:
            raise DeploymentError("ministack_identity_conflict")
        return item

    def check_engine(self):
        validate_registry_host()
        context = json.loads(self.run("docker", "context", "inspect"))[0]
        if os.environ.get("DOCKER_HOST") or not context["Endpoints"]["docker"]["Host"].startswith(("npipe://", "unix://")):
            raise DeploymentError("local_docker_engine_required")
        if self.run("docker", "info", "--format", "{{.OSType}}") != "linux":
            raise DeploymentError("linux_containers_required")

    def preflight(self):
        self.check_engine()
        owned = [self.inspect(i) for i in self.ids()]
        registry = self.registry_id()
        if registry:
            owned.append(self.owned_registry(registry))
        for port in (4566, 18000, 15173):
            with socket.socket() as sock:
                sock.settimeout(1)
                occupied = sock.connect_ex(("127.0.0.1", port)) == 0
            if occupied and not any(
                b.get("HostIp") == "127.0.0.1" and b.get("HostPort") == str(port)
                for item in owned for values in (item["NetworkSettings"].get("Ports") or {}).values()
                for b in (values or [])
            ):
                raise DeploymentError("deployment_port_conflict")

    def ensure_volumes(self):
        existing = self.run("docker", "volume", "ls", "--format", "{{.Name}}", operation="docker_volume").splitlines()
        for name in VOLUMES:
            if name not in existing:
                self.run("docker", "volume", "create", "--label", LABEL + "=" + TARGET, name, operation="docker_volume")
            item = self.inspect(name, "volume")
            if (item.get("Labels") or {}).get(LABEL) != TARGET:
                raise DeploymentError("persistent_volume_ownership_mismatch")

    def verify_registry(self):
        identifier = self.registry_id()
        if not identifier:
            raise DeploymentError("ministack_missing")
        item = self.owned_registry(identifier)
        expected = self.inspect(MINISTACK_IMAGE, "image")["Id"]
        if item["Image"] != expected:
            raise DeploymentError("ministack_image_mismatch")
        mounts = item.get("Mounts", [])
        mapped = {m["Destination"]: m for m in mounts}
        # The pinned image declares two empty init-hook anonymous volumes.
        allowed = {"/state", "/docker-entrypoint-initaws.d", "/etc/localstack/init"}
        if (set(mapped) - allowed or mapped.get("/state", {}).get("Name") != VOLUMES[2]
                or any(m.get("Type") != "volume" for m in mounts)):
            raise DeploymentError("ministack_mount_mismatch")
        required = {"PERSIST_STATE=1", "STATE_DIR=/state", "SERVICES=ecr"}
        if not required.issubset(set(item["Config"].get("Env", []))):
            raise DeploymentError("ministack_configuration_mismatch")
        try:
            check_bindings(item, {"4566/tcp": 4566})
        except DeploymentError:
            if item["State"]["Running"]:
                self.run("docker", "stop", identifier, operation="docker_compose_stop")
            raise
        if not item["State"]["Running"]:
            raise DeploymentError("ministack_not_running")

    def start_registry(self):
        self.ensure_volumes()
        identifier = self.registry_id()
        if not identifier:
            self.run("docker", "pull", MINISTACK_IMAGE, operation="docker_pull", timeout=300)
            self.run("docker", "create", "--name", MINISTACK, "--label", LABEL + "=" + TARGET,
                     "-p", "127.0.0.1:4566:4566", "-e", "SERVICES=ecr", "-e", "PERSIST_STATE=1",
                     "-e", "STATE_DIR=/state", "-v", VOLUMES[2] + ":/state", MINISTACK_IMAGE,
                     operation="docker_create")
            identifier = self.registry_id()
        item = self.owned_registry(identifier)
        # Validate stopped-container identity/mounts before executing it.
        if (item["Config"]["Image"] != MINISTACK_IMAGE
                or item["HostConfig"].get("Privileged")
                or item["HostConfig"].get("Binds") != [VOLUMES[2] + ":/state"]
                or item["HostConfig"].get("PortBindings") != {"4566/tcp": [{"HostIp": "127.0.0.1", "HostPort": "4566"}]}):
            raise DeploymentError("ministack_configuration_mismatch")
        if not item["State"]["Running"]:
            self.run("docker", "start", identifier, operation="docker_start")
        self.verify_registry()
        health([(ENDPOINT + "/_ministack/health", False)], timeout=60)

    def build(self, candidate):
        for service in PORTS:
            args = ["docker", "build", "--label", "org.opencontainers.image.revision=" + candidate,
                    "-t", f"sf-demo-aws-{service}:{candidate}"]
            if service == "backend":
                args += ["-f", str(self.source / "Dockerfile.backend"), str(self.source)]
            else:
                args += ["--build-arg", "VITE_API_BASE_URL=http://127.0.0.1:18000",
                         "-f", str(self.source / "frontend/Dockerfile"), str(self.source / "frontend")]
            self.run(*args, operation="docker_build", timeout=1800)

    def push(self, service, candidate, ecr):
        validate_registry_host()
        tag = REGISTRY + "/" + REPOSITORIES[service] + ":" + candidate
        self.run("docker", "tag", f"sf-demo-aws-{service}:{candidate}", tag, operation="docker_tag")
        self.run("docker", "push", tag, operation="docker_push", timeout=600)
        value = ecr.find(service, candidate)
        reference = image_reference(service, value)
        item = self.inspect(tag, "image")
        if reference not in item.get("RepoDigests", []):
            raise DeploymentError("ecr_digest_mismatch")
        return value

    def pull(self, service, candidate, value):
        validate_registry_host()
        reference = image_reference(service, value)
        self.run("docker", "pull", reference, operation="docker_pull", timeout=600)
        item = self.inspect(reference, "image")
        if (reference not in item.get("RepoDigests", [])
                or (item["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") != candidate):
            raise DeploymentError("pulled_image_identity_mismatch")
        return item["Id"]

    def render(self, digests):
        env_file = self.home / "config/.env.production"
        extra = {"SF_BACKEND_IMAGE": image_reference("backend", digests["backend"]),
                 "SF_FRONTEND_IMAGE": image_reference("frontend", digests["frontend"]),
                 "SF_BACKEND_ENV_FILE": str(env_file)}
        config = json.loads(self.run("docker", "compose", "-p", PROJECT, "-f", str(self.source / COMPOSE),
                           "config", "--format", "json", "--no-env-resolution", extra=extra,
                           operation="docker_compose_config"))
        if config.get("name") != PROJECT or set(config["services"]) != set(PORTS):
            raise DeploymentError("unexpected_compose_services")
        for service, (host_port, container_port) in PORTS.items():
            item = config["services"][service]
            if item.get("image") != image_reference(service, digests[service]) or "build" in item:
                raise DeploymentError("compose_image_mismatch")
            ports = item.get("ports", [])
            if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1"
                    or str(ports[0]["published"]) != str(host_port)
                    or str(ports[0]["target"]) != container_port.split("/")[0]):
                raise DeploymentError("non_loopback_compose_port")
        if {v["name"] for v in config["volumes"].values()} != set(VOLUMES[:2]):
            raise DeploymentError("persistent_volume_mismatch")
        return config

    def compose(self, *args, timeout=120, operation="docker_compose_up"):
        return self.run("docker", "compose", "-p", PROJECT, "-f", str(self.config), *args,
                        operation=operation, timeout=timeout)

    def verify(self, candidate, image_ids):
        self.verify_registry()
        containers = [self.inspect(i) for i in self.ids()]
        if len(containers) != 2:
            raise DeploymentError("deployment_container_count_mismatch")
        for service, (host_port, container_port) in PORTS.items():
            matched = [c for c in containers if c["Config"].get("Labels", {}).get("com.docker.compose.service") == service]
            if len(matched) != 1:
                raise DeploymentError("deployment_container_missing")
            item = matched[0]
            labels = item["Config"].get("Labels", {})
            if labels.get("com.docker.compose.project") != PROJECT:
                raise DeploymentError("deployment_identity_mismatch")
            try:
                check_bindings(item, {container_port: host_port})
            except DeploymentError:
                self.run("docker", "stop", item["Id"], operation="docker_compose_stop")
                raise
            if (not item["State"]["Running"] or item["Image"] != image_ids[service]
                    or labels.get("org.opencontainers.image.revision") != candidate):
                raise DeploymentError("running_image_mismatch")
            if service == "backend":
                mounts = {m["Destination"]: m.get("Name") for m in item["Mounts"]}
                if mounts.get("/app/workspace") != VOLUMES[0] or mounts.get("/app/data") != VOLUMES[1]:
                    raise DeploymentError("persistent_volume_mismatch")

    def stop(self):
        self.check_engine()
        ids = self.ids()
        if ids:
            self.run("docker", "stop", *ids, operation="docker_compose_stop", timeout=120)
        identifier = self.registry_id()
        if identifier:
            self.owned_registry(identifier)
            self.run("docker", "stop", "-t", "30", identifier, operation="docker_compose_stop")


def validate(home, source, candidate):
    if home.is_relative_to(source) or source.is_relative_to(home):
        raise DeploymentError("deployment_home_must_be_separate")
    local.validate_source(source, candidate)
    if (source / COMPOSE).read_bytes() != (ROOT / COMPOSE).read_bytes():
        raise DeploymentError("candidate_compose_contract_changed")
    local.validate_evidence(home, candidate)
    local.validate_window(home, candidate)
    if not (home / "config/.env.production").is_file():
        raise DeploymentError("external_backend_configuration_missing")


def deploy(home, source, candidate, operation, attempt_id, runtime_factory=Runtime, ecr_factory=ECR):
    with deployment_lock(home):
        journal = Journal(home, TARGET)
        attempt = journal.begin(attempt_id, candidate, operation)
        if attempt is None:
            print("Deployment attempt already completed; no side effects repeated.")
            return
        stage = "validate"
        try:
            local.select_target(journal, candidate, operation)
            validate(home, source, candidate)
            runtime = runtime_factory(source, home)
            runtime.preflight()
            for folder in ("validated", "config", "ministack-state"):
                (home / folder).mkdir(exist_ok=True)
            stage = "ministack"
            local.validate_window(home, candidate)
            runtime.start_registry()
            stage = "registry"
            ecr = ecr_factory()
            for service in PORTS:
                local.validate_window(home, candidate)
                ecr.ensure(service)
            recorded = next((a for a in reversed(journal.data["attempts"])
                             if a["deployed_sha"] == candidate and a["status"] in ("healthy", "rolled_back")), None)
            digests = {}
            if recorded:
                for service in PORTS:
                    value = recorded.get(service + "_digest")
                    image_reference(service, value)
                    if ecr.find(service, candidate) != value:
                        raise DeploymentError("retained_images_missing_or_changed")
                    digests[service] = value
            else:
                if operation == "rollback":
                    raise DeploymentError("rollback_images_missing")
                if any(ecr.find(s, candidate) is not None for s in PORTS):
                    raise DeploymentError("unrecorded_image_exists")
                stage = "build"
                local.validate_window(home, candidate)
                runtime.build(candidate)
                stage = "push"
                for service in PORTS:
                    local.validate_window(home, candidate)
                    digests[service] = runtime.push(service, candidate, ecr)
            # Pull failures belong to deploy stage; no additional stage contract.
            stage = "deploy"
            images = {s: runtime.pull(s, candidate, digests[s]) for s in PORTS}
            attempt.update({s + "_digest": digests[s] for s in PORTS})
            attempt["compose_project"] = PROJECT
            journal.save()
            with tempfile.TemporaryDirectory(prefix="compose-", dir=home) as temporary:
                runtime.config = Path(temporary) / "compose.json"
                atomic_write(runtime.config, runtime.render(digests))
                local.validate_window(home, candidate)
                runtime.preflight()
                journal.deploying(attempt)
                # One writer only: do not overlap old/new Backend SQLite writers.
                runtime.compose("stop", operation="docker_compose_stop")
                runtime.compose("up", "-d", "--no-build", "--pull", "never", timeout=180)
                stage = "health"
                runtime.verify(candidate, images)
                health([("http://127.0.0.1:18000/health", True), ("http://127.0.0.1:15173/", False)])
                runtime.verify(candidate, images)
            stage = "metadata"
            journal.finish(attempt, True)
            print(json.dumps({k: attempt[k] for k in ("environment", "target", "deployed_sha", "status",
                             "current_sha", "previous_sha", "backend_digest", "frontend_digest")}))
        except Exception as error:
            code = str(error) if isinstance(error, DeploymentError) else "deployment_failed"
            command_operation = error.operation if isinstance(error, DeploymentError) else None
            try:
                persisted = Journal(home, TARGET)
                pending = next(a for a in persisted.data["attempts"] if a["attempt_id"] == attempt_id)
                persisted.finish(pending, False, f"{stage}_failed", reason_code=code, stage=stage,
                                 command_operation=command_operation)
            except Exception:
                print("Failure diagnostic could not be persisted.")
            raise DeploymentError(code, stage=stage, operation=command_operation) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("select", "run", "stop"))
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    try:
        home, candidate, operation = settings(args.action != "stop")
        if args.action == "select":
            local.select_target(Journal(home, TARGET), candidate, operation)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                    stream.write(f"sha={candidate}\n")
            print(f"Selected target={TARGET} sha={candidate}")
        elif args.action == "stop":
            with deployment_lock(home):
                Runtime(ROOT, home).stop()
            print("Target stopped; volumes, registry state and journal retained.")
        else:
            if args.source is None:
                raise DeploymentError("source_checkout_required")
            deploy(home, args.source.resolve(), candidate, operation, os.environ.get("CD_ATTEMPT_ID", ""))
    except Exception as error:
        print("Deployment failed: " + (str(error) if isinstance(error, DeploymentError) else "deployment_failed"))
        if isinstance(error, DeploymentError):
            if error.stage:
                print("Stage: " + error.stage)
            if error.operation:
                print("Command operation: " + error.operation)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
