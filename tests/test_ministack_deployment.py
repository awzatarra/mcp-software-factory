import copy
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/deployment"
for name in ("deployment_metadata", "deploy_local", "ministack_ecr", "deploy_ministack"):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
md, ecr, cd = (sys.modules[n] for n in ("deployment_metadata", "ministack_ecr", "deploy_ministack"))
A, B = "a" * 40, "b" * 40
DA, DB = "sha256:" + "1" * 64, "sha256:" + "2" * 64


class Response(io.BytesIO):
    status = 200

    def geturl(self):
        return ecr.ENDPOINT


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return Response(json.dumps(reply).encode())


@pytest.mark.parametrize("endpoint", [None, "", "https://ecr.us-east-1.amazonaws.com", "http://localhost:4566", "http://127.0.0.1:4566/", "http://127.0.0.1:4566@evil.example"])
def test_endpoint_fail_closed(endpoint):
    with pytest.raises(md.DeploymentError, match="non_local_aws_endpoint"):
        ecr.ECR(endpoint)


def test_dummy_environment_ignores_credentials_and_profiles(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ENDPOINT_URL"):
        monkeypatch.setenv(key, "private")
    env = ecr.isolated_environment()
    assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == "test"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert set(k for k in env if k.startswith("AWS_")) == {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "AWS_EC2_METADATA_DISABLED"}


def test_ensure_existing_repository_is_read_only():
    opener = Opener([{"repositories": [{"repositoryName": ecr.REPOSITORIES["backend"]}]}])
    assert ecr.ECR(opener=opener).ensure("backend") == ecr.REPOSITORIES["backend"]
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == ecr.ENDPOINT
    assert not opener.requests[0].has_header("Authorization")


def test_ensure_creates_only_missing_repository():
    missing = urllib.error.HTTPError(ecr.ENDPOINT, 400, "hidden", {}, io.BytesIO(b'{"__type":"RepositoryNotFoundException"}'))
    opener = Opener([missing, {"repository": {"repositoryName": ecr.REPOSITORIES["backend"]}}])
    ecr.ECR(opener=opener).ensure("backend")
    assert len(opener.requests) == 2
    assert json.loads(opener.requests[1].data)["imageTagMutability"] == "IMMUTABLE"


def test_ensure_other_error_never_creates_repository():
    opener = Opener([urllib.error.HTTPError(ecr.ENDPOINT, 500, "private", {}, io.BytesIO(b'private'))])
    with pytest.raises(md.DeploymentError, match="^ecr_operation_failed$") as error:
        ecr.ECR(opener=opener).ensure("backend")
    assert error.value.operation == "ecr_describe_repositories"
    assert len(opener.requests) == 1


def test_redirect_rejected():
    with pytest.raises(md.DeploymentError, match="non_local_aws_endpoint"):
        ecr.NoRedirect().redirect_request(None)


@pytest.mark.parametrize("value", ["latest", "sha256:abc", None, "https://example.com/image", "sha256:" + "A" * 64])
def test_invalid_digest(value):
    with pytest.raises(md.DeploymentError, match="invalid_image_digest"):
        ecr.image_reference("backend", value)


def test_external_registry_rejected():
    with pytest.raises(md.DeploymentError, match="non_local_registry"):
        ecr.validate_reference("backend", "example.com/backend@" + DA)
    assert ecr.validate_reference("backend", ecr.image_reference("backend", DA))


def test_localhost_cannot_resolve_remotely(monkeypatch):
    monkeypatch.setattr(ecr.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("203.0.113.10", 4566))])
    with pytest.raises(md.DeploymentError, match="non_local_registry"):
        ecr.validate_registry_host()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::1", ""])
def test_wildcard_binding_rejected(host):
    item = {"NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": host, "HostPort": "18000"}]}}}
    with pytest.raises(md.DeploymentError, match="non_loopback"):
        cd.check_bindings(item, {"8000/tcp": 18000})


def test_bindings_require_all_expected_and_no_extra():
    item = {"NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18000"}]}}}
    cd.check_bindings(item, {"8000/tcp": 18000})
    item["NetworkSettings"]["Ports"]["9000/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "9000"}]
    with pytest.raises(md.DeploymentError, match="binding_mismatch"):
        cd.check_bindings(item, {"8000/tcp": 18000})


def test_home_is_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ENVIRONMENT", cd.TARGET)
    monkeypatch.setenv("CD_COMMIT_SHA", A)
    monkeypatch.setenv("SF_MINISTACK_DEPLOY_HOME", str(tmp_path / "target"))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "checkout"))
    assert cd.settings()[0] == tmp_path / "target"
    for value in (str(tmp_path / "checkout/home"), str(tmp_path), "relative"):
        monkeypatch.setenv("SF_MINISTACK_DEPLOY_HOME", value)
        with pytest.raises(md.DeploymentError):
            cd.settings()


def test_demo_local_home_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CD_ENVIRONMENT", cd.TARGET)
    monkeypatch.setenv("SF_DEPLOY_HOME", str(tmp_path / "local"))
    monkeypatch.setenv("SF_MINISTACK_DEPLOY_HOME", str(tmp_path / "local/subdir"))
    with pytest.raises(md.DeploymentError, match="separate"):
        cd.settings(False)


def test_journals_cannot_cross_targets(tmp_path):
    journal = md.Journal(tmp_path, cd.TARGET)
    attempt = journal.begin("one", A, "deploy")
    assert attempt["target"] == cd.TARGET
    with pytest.raises(md.DeploymentError, match="invalid_deployment_metadata"):
        md.Journal(tmp_path)


class FakeECR:
    def __init__(self):
        self.images = {}

    def ensure(self, service):
        return service

    def find(self, service, candidate):
        return self.images.get((service, candidate))


class FakeRuntime:
    def __init__(self, source, home):
        self.calls = []

    def preflight(self):
        self.calls.append("preflight")

    def start_registry(self):
        self.calls.append("start_registry")

    def build(self, candidate):
        self.calls.append("build")

    def push(self, service, candidate, client):
        value = DA if candidate == A else DB
        client.images[service, candidate] = value
        self.calls.append("push")
        return value

    def pull(self, service, candidate, value):
        self.calls.append("pull")
        return service + value

    def render(self, values):
        return {"images": values}

    def compose(self, *args, **kwargs):
        self.calls.append(args[0])

    def verify(self, candidate, values):
        self.calls.append("verify")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "validate", lambda *args: None)
    monkeypatch.setattr(cd.local, "validate_window", lambda *args: None)
    monkeypatch.setattr(cd, "health", lambda *args: None)
    runtime, client = FakeRuntime(None, None), FakeECR()

    def run(candidate, operation, attempt):
        cd.deploy(tmp_path, tmp_path / "source", candidate, operation, attempt,
                  runtime_factory=lambda *args: runtime, ecr_factory=lambda: client)
        return md.Journal(tmp_path, cd.TARGET).data

    return run, runtime, client


def test_first_same_new_rollback_and_idempotency(harness):
    run, runtime, client = harness
    data = run(A, "deploy", "1")
    assert data["current_sha"] == A and data["previous_sha"] is None
    assert data["attempts"][-1]["backend_digest"] == DA
    runtime.calls.clear()
    data = run(A, "deploy", "2")
    assert "build" not in runtime.calls and "push" not in runtime.calls
    assert data["previous_sha"] is None
    data = run(B, "deploy", "3")
    assert (data["current_sha"], data["previous_sha"]) == (B, A)
    runtime.calls.clear()
    data = run(A, "rollback", "4")
    assert (data["current_sha"], data["previous_sha"]) == (A, B)
    assert data["attempts"][-1]["status"] == "rolled_back"
    assert "build" not in runtime.calls
    runtime.calls.clear()
    assert run(A, "rollback", "4") == data
    assert runtime.calls == []


def test_digest_mismatch_preserves_pointers(harness, tmp_path):
    run, runtime, client = harness
    run(A, "deploy", "1")
    client.images["backend", A] = DB
    with pytest.raises(md.DeploymentError, match="retained_images_missing_or_changed"):
        run(A, "deploy", "2")
    data = md.Journal(tmp_path, cd.TARGET).data
    assert (data["current_sha"], data["previous_sha"]) == (A, None)
    assert data["status"] == "failed"


@pytest.mark.parametrize("method,stage", [("build", "build"), ("push", "push"), ("pull", "deploy"), ("verify", "health")])
def test_failures_never_advance_pointers(harness, tmp_path, monkeypatch, method, stage):
    run, runtime, client = harness
    run(A, "deploy", "1")

    def fail(*args):
        raise md.DeploymentError("deployment_command_failed", operation="docker_pull")

    monkeypatch.setattr(runtime, method, fail)
    with pytest.raises(md.DeploymentError):
        run(B, "deploy", "2")
    data = md.Journal(tmp_path, cd.TARGET).data
    last = data["attempts"][-1]
    assert data["current_sha"] == A and data["previous_sha"] is None
    assert last["reason"] == stage + "_failed"
    assert last["reason_code"] == "deployment_command_failed"
    assert last["command_operation"] == "docker_pull"


def test_unexpected_exception_is_sanitized(harness, tmp_path, monkeypatch):
    run, runtime, client = harness
    monkeypatch.setattr(runtime, "build", lambda *a: (_ for _ in ()).throw(ValueError("secret")))
    with pytest.raises(md.DeploymentError, match="^deployment_failed$"):
        run(A, "deploy", "1")
    assert "secret" not in (tmp_path / "deployment.json").read_text()


def test_pull_checks_digest_and_sha(tmp_path, monkeypatch):
    runtime = cd.Runtime(tmp_path, tmp_path)
    monkeypatch.setattr(runtime, "run", lambda *a, **k: "")
    item = {"Id": "image", "RepoDigests": [ecr.image_reference("backend", DA)],
            "Config": {"Labels": {"org.opencontainers.image.revision": A}}}
    monkeypatch.setattr(runtime, "inspect", lambda *a: item)
    assert runtime.pull("backend", A, DA) == "image"
    with pytest.raises(md.DeploymentError, match="identity_mismatch"):
        runtime.pull("backend", B, DA)
    item["RepoDigests"] = []
    with pytest.raises(md.DeploymentError, match="identity_mismatch"):
        runtime.pull("backend", A, DA)


def test_wildcard_stops_only_owned_affected_container(tmp_path, monkeypatch):
    runtime = cd.Runtime(tmp_path, tmp_path)
    calls = []
    backend = {"Id": "owned-backend", "Image": "backend-image", "State": {"Running": True},
               "Config": {"Labels": {"com.docker.compose.project": cd.PROJECT,
                                      "com.docker.compose.service": "backend",
                                      "org.opencontainers.image.revision": A}},
               "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18000"}]}}}
    frontend = copy.deepcopy(backend)
    frontend["Id"] = "owned-frontend"
    frontend["Config"]["Labels"]["com.docker.compose.service"] = "frontend"
    monkeypatch.setattr(runtime, "verify_registry", lambda: None)
    monkeypatch.setattr(runtime, "ids", lambda: ["backend", "frontend"])
    monkeypatch.setattr(runtime, "inspect", lambda identifier: backend if identifier == "backend" else frontend)
    monkeypatch.setattr(runtime, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(md.DeploymentError, match="non_loopback"):
        runtime.verify(A, {"backend": "backend-image", "frontend": "frontend-image"})
    assert calls == [("docker", "stop", "owned-backend")]


def test_stop_preserves_data_and_only_stops_target(tmp_path, monkeypatch):
    runtime = cd.Runtime(tmp_path, tmp_path)
    calls = []
    monkeypatch.setattr(runtime, "check_engine", lambda: None)
    monkeypatch.setattr(runtime, "ids", lambda: ["owned-app"])
    monkeypatch.setattr(runtime, "registry_id", lambda: "owned-ecr")
    monkeypatch.setattr(runtime, "owned_registry", lambda identifier: {})
    monkeypatch.setattr(runtime, "run", lambda *args, **kwargs: calls.append(args))
    runtime.stop()
    assert calls == [("docker", "stop", "owned-app"), ("docker", "stop", "-t", "30", "owned-ecr")]


def test_rollback_must_equal_previous(harness):
    run, runtime, client = harness
    run(A, "deploy", "1")
    runtime.calls.clear()
    with pytest.raises(md.DeploymentError, match="previous_sha_missing"):
        run(A, "rollback", "2")
    assert runtime.calls == []


def test_expired_window_blocks_mutations(harness, monkeypatch):
    run, runtime, client = harness
    def expired(*args):
        raise md.DeploymentError("maintenance_window_not_confirmed")
    monkeypatch.setattr(cd.local, "validate_window", expired)
    with pytest.raises(md.DeploymentError, match="maintenance_window"):
        run(A, "deploy", "1")
    assert runtime.calls == ["preflight"]


def test_metadata_write_failure_preserves_confirmed_pointers(harness, tmp_path, monkeypatch):
    run, runtime, client = harness
    run(A, "deploy", "1")
    save = md.Journal.save
    def fail_success(journal):
        if journal.data["current_sha"] == B:
            raise OSError("private storage path")
        return save(journal)
    monkeypatch.setattr(md.Journal, "save", fail_success)
    with pytest.raises(md.DeploymentError, match="^deployment_failed$"):
        run(B, "deploy", "2")
    data = md.Journal(tmp_path, cd.TARGET).data
    assert data["current_sha"] == A and data["previous_sha"] is None
    assert data["attempts"][-1]["reason"] == "metadata_failed"
    assert "private storage path" not in (tmp_path / "deployment.json").read_text()


def test_external_config_required_before_runtime(tmp_path, monkeypatch):
    home, source = tmp_path / "home", tmp_path / "source"
    home.mkdir()
    source.mkdir()
    (source / cd.COMPOSE).write_bytes((cd.ROOT / cd.COMPOSE).read_bytes())
    for name in ("validate_source", "validate_evidence", "validate_window"):
        monkeypatch.setattr(cd.local, name, lambda *args: None)
    with pytest.raises(md.DeploymentError, match="external_backend_configuration_missing"):
        cd.validate(home, source, A)


def test_workflow_and_compose_contract():
    import yaml
    root = SCRIPTS.parents[1]
    workflow = yaml.load((root / ".github/workflows/software-factory-deploy-ministack.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "software-factory-deploy-ministack", "cancel-in-progress": "false"}
    compose = yaml.safe_load((root / cd.COMPOSE).read_text())
    assert compose["name"] == cd.PROJECT
    assert set(compose["services"]) == {"backend", "frontend"}
    for item in compose["services"].values():
        assert "build" not in item
        assert all(p.startswith("127.0.0.1:") for p in item["ports"])
    assert {v["name"] for v in compose["volumes"].values()} == set(cd.VOLUMES[:2])
