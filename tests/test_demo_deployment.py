import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "deployment"
for name in ("deployment_metadata", "deploy_local"):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
cd = sys.modules["deploy_local"]
md = sys.modules["deployment_metadata"]
A, B, C = "a" * 40, "b" * 40, "c" * 40


def complete(home, candidate, operation, identity):
    journal = md.Journal(home)
    attempt = journal.begin(identity, candidate, operation)
    journal.deploying(attempt)
    journal.finish(attempt, True)
    return md.Journal(home)


def test_first_same_new_and_rollback_restart(tmp_path):
    first = complete(tmp_path, A, "deploy", "1")
    assert (first.data["current_sha"], first.data["previous_sha"]) == (A, None)
    second = complete(tmp_path, A, "deploy", "2")
    assert second.data["previous_sha"] is None
    third = complete(tmp_path, B, "deploy", "3")
    assert (third.data["current_sha"], third.data["previous_sha"]) == (B, A)
    same = complete(tmp_path, B, "deploy", "4")
    assert same.data["previous_sha"] == A
    rolled = complete(tmp_path, A, "rollback", "5")
    assert (rolled.data["current_sha"], rolled.data["previous_sha"]) == (A, B)
    assert rolled.data["attempts"][-1]["status"] == "rolled_back"
    assert rolled.data["attempts"][-1]["deployed_at"] is not None
    before = copy.deepcopy(rolled.data)
    assert rolled.begin("5", A, "rollback") is None
    assert md.Journal(tmp_path).data == before


def test_failure_and_interrupted_attempt_preserve_pointers(tmp_path):
    journal = complete(tmp_path, A, "deploy", "1")
    abandoned = journal.begin("2", B, "deploy")
    journal.deploying(abandoned)
    restarted = md.Journal(tmp_path)
    current = restarted.begin("3", C, "deploy")
    assert restarted.data["attempts"][1]["reason"] == "interrupted_attempt"
    restarted.finish(current, False, "health_failed")
    assert md.Journal(tmp_path).data["current_sha"] == A
    assert current["deployed_at"] is None
    assert current["status"] == "failed"


def test_attempt_conflict_and_missing_stale_rollback(tmp_path):
    journal = complete(tmp_path, A, "deploy", "1")
    with pytest.raises(md.DeploymentError, match="identity_conflict"):
        journal.begin("1", B, "deploy")
    with pytest.raises(md.DeploymentError, match="previous_sha_missing"):
        cd.select_target(journal, A, "rollback")
    journal = complete(tmp_path, B, "deploy", "2")
    with pytest.raises(md.DeploymentError, match="sha_mismatch"):
        cd.select_target(journal, B, "rollback")
    assert cd.select_target(journal, A, "rollback") == A


def test_atomic_write_failure_leaves_original(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    md.atomic_write(target, {"old": True})
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(md.os, "replace", fail)
    with pytest.raises(OSError):
        md.atomic_write(target, {"old": False})
    assert md.read_json(target) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_process_lock_and_release(tmp_path):
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from deployment_metadata import deployment_lock; "
        "with_lock = deployment_lock(sys.argv[2]); with_lock.__enter__()"
    )
    with md.deployment_lock(tmp_path):
        blocked = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(tmp_path)], capture_output=True)
        assert blocked.returncode != 0
        assert b"deployment_already_running" in blocked.stderr
    released = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(tmp_path)], capture_output=True)
    assert released.returncode == 0


@pytest.mark.parametrize("value", ["HEAD", "master", "a" * 7, "../master", "a;whoami", "", None])
def test_only_full_sha(value):
    with pytest.raises(md.DeploymentError):
        md.sha(value)


def test_source_validation_checks_exact_sha_clean_and_remote(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_bytes((cd.CONTROLLER_ROOT / "docker-compose.yml").read_bytes())
    responses = {"HEAD": A, "origin": f"https://github.com/{cd.REPOSITORY}.git",
                 "refs/remotes/origin/master": "", "--ignored": "", "ls-files": "README.md"}
    monkeypatch.setattr(cd, "command", lambda args, cwd, **kw: responses[args[-1]])
    cd.validate_source(tmp_path, A)
    responses["--ignored"] = "?? .env"
    with pytest.raises(md.DeploymentError, match="not_clean"):
        cd.validate_source(tmp_path, A)
    responses["--ignored"] = ""
    responses["HEAD"] = B
    with pytest.raises(md.DeploymentError, match="sha_mismatch"):
        cd.validate_source(tmp_path, A)
    responses["HEAD"] = A
    responses["origin"] = "https://github.com/other/repo"
    with pytest.raises(md.DeploymentError, match="unexpected_source"):
        cd.validate_source(tmp_path, A)


def test_evidence_is_platform_specific_and_sha_bound(tmp_path):
    evidence = {"repository": cd.REPOSITORY, "commit_sha": A, "status": "passed",
                "kind": "operator_platform_validation", "stores_compatible": True,
                "validated_by": "operator", "evidence_reference": "local-checks",
                "validated_at": datetime.now(timezone.utc).isoformat(),
                "checks": [{"name": "existing checks", "result": "passed"}]}
    target = tmp_path / "validated" / f"{A}.json"
    md.atomic_write(target, evidence)
    cd.validate_evidence(tmp_path, A)
    for field, bad in (("commit_sha", B), ("kind", "project_ci"), ("stores_compatible", False),
                       ("checks", []), ("validated_at", "invalid")):
        md.atomic_write(target, {**evidence, field: bad})
        with pytest.raises(md.DeploymentError):
            cd.validate_evidence(tmp_path, A)


class Response:
    def __init__(self, url, ok=True):
        self.url, self.status, self.ok = url, 200, ok
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def geturl(self):
        return self.url
    def read(self, count):
        return b'{"status":"ok"}' if self.ok else b'{"status":"failed"}'


def test_health_both_services_and_bounded_failure():
    calls, elapsed = [], [0]
    def opener(url, timeout):
        calls.append((url, timeout))
        return Response(url)
    cd.health_check(opener=opener)
    assert len(calls) == 2
    def sleep(seconds):
        elapsed[0] += seconds
    with pytest.raises(md.DeploymentError, match="health_timeout"):
        cd.health_check(timeout=12, clock=lambda: elapsed[0], sleep=sleep,
                        opener=lambda url, timeout: Response(url, False))
    assert elapsed[0] == 12


@pytest.fixture
def simulated(monkeypatch, tmp_path):
    events = []
    for name in ("validate_source", "validate_evidence", "validate_window"):
        monkeypatch.setattr(cd, name, lambda *args: None)
    monkeypatch.setattr(cd, "compose_config", lambda *args: {})
    class FakeDocker:
        def __init__(self, *args):
            pass
        def preflight(self):
            events.append("preflight")
        def compose(self, *args, **kwargs):
            events.append(args[0])
        def image_ids(self, candidate):
            return {"backend": candidate + "-backend", "frontend": candidate + "-frontend"}
        def verify_running(self, images):
            events.append("verify")
    return events, FakeDocker


def test_deployment_sequence_and_retained_images(tmp_path, simulated):
    events, docker = simulated
    for candidate, operation, identity in ((A, "deploy", "1"), (A, "deploy", "2"),
                                            (B, "deploy", "3"), (A, "rollback", "4")):
        cd.deploy(tmp_path, tmp_path, candidate, operation, identity, docker, lambda: events.append("health"))
    assert events.count("build") == 2
    assert events.count("up") == events.count("health") == 4
    before = list(events)
    cd.deploy(tmp_path, tmp_path, A, "rollback", "4", docker, lambda: events.append("health"))
    assert events == before
    assert md.Journal(tmp_path).data["current_sha"] == A


@pytest.mark.parametrize("failure", ["build", "up", "verify", "health"])
def test_failure_never_advances_current(tmp_path, simulated, failure):
    events, base = simulated
    complete(tmp_path, A, "deploy", "old")
    class Failing(base):
        def compose(self, *args, **kwargs):
            if args[0] == failure:
                raise OSError("secret=must-not-print")
            super().compose(*args, **kwargs)
        def verify_running(self, images):
            if failure == "verify":
                raise OSError("wrong image")
    def health():
        if failure == "health":
            raise OSError("bad health")
    with pytest.raises(md.DeploymentError) as error:
        cd.deploy(tmp_path, tmp_path, B, "deploy", "new", Failing, health)
    assert "secret" not in str(error.value)
    state = md.Journal(tmp_path).data
    assert state["status"] == "failed"
    assert state["current_sha"] == A
    assert state["attempts"][-1]["deployed_at"] is None


def test_config_preserves_existing_architecture_and_external_env(monkeypatch, tmp_path):
    config = {"services": {"backend": {"build": {}, "ports": [
        {"host_ip": "127.0.0.1", "published": "8000", "target": 8000}]},
        "frontend": {"build": {}, "ports": [
        {"host_ip": "127.0.0.1", "published": "5173", "target": 80}]}},
        "volumes": {k: {"name": v} for k, v in cd.VOLUMES.items()}}
    env_file = tmp_path / "config" / ".env.production"
    env_file.parent.mkdir()
    env_file.write_text("OPENAI_API_KEY=not-for-output")
    monkeypatch.setattr(cd, "command", lambda *args: json.dumps(config))
    actual = cd.compose_config(cd.CONTROLLER_ROOT, tmp_path, A)
    assert "not-for-output" not in json.dumps(actual)
    assert actual["services"]["backend"]["env_file"][0]["path"] == str(env_file)
    assert actual["volumes"] == config["volumes"]
    assert actual["services"]["frontend"]["image"].endswith(A)
    config["services"]["backend"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(md.DeploymentError, match="non_loopback"):
        cd.compose_config(cd.CONTROLLER_ROOT, tmp_path, A)


def test_maintenance_is_sha_bound_and_expiring(tmp_path):
    window = {"commit_sha": A, "quiescent": True,
              "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    md.atomic_write(tmp_path / "maintenance.json", window)
    cd.validate_window(tmp_path, A)
    with pytest.raises(md.DeploymentError):
        cd.validate_window(tmp_path, B)
    window["expires_at"] = "2000-01-01T00:00:00+00:00"
    md.atomic_write(tmp_path / "maintenance.json", window)
    with pytest.raises(md.DeploymentError):
        cd.validate_window(tmp_path, A)


def test_running_container_must_match_built_image(monkeypatch, tmp_path):
    docker = cd.Docker(tmp_path, tmp_path / "compose.json")
    monkeypatch.setattr(docker, "compose", lambda *args: "container-id")
    item = {"Image": "sha256:correct", "State": {"Running": True},
            "Config": {"Labels": {"com.docker.compose.project": cd.PROJECT}}}
    monkeypatch.setattr(docker, "inspect", lambda *args: [item])
    docker.verify_running({"backend": "sha256:correct"})
    with pytest.raises(md.DeploymentError, match="running_image_mismatch"):
        docker.verify_running({"backend": "sha256:old"})


def test_metadata_failure_fails_job_without_advancing_pointers(tmp_path, simulated, monkeypatch):
    events, docker = simulated
    complete(tmp_path, A, "deploy", "old")
    original = md.atomic_write
    def write(path, data):
        if data.get("current_sha") == B:
            raise OSError("disk error")
        original(path, data)
    monkeypatch.setattr(md, "atomic_write", write)
    with pytest.raises(md.DeploymentError, match="deployment_failed") as error:
        cd.deploy(tmp_path, tmp_path, B, "deploy", "new", docker, lambda: None)
    assert error.value.stage == "metadata"
    state = md.Journal(tmp_path).data
    assert state["current_sha"] == A
    assert state["status"] == "failed"
    assert state["attempts"][-1]["reason"] == "metadata_failed"
    assert state["attempts"][-1]["reason_code"] == "deployment_failed"


def test_external_home_required(monkeypatch, tmp_path):
    monkeypatch.setenv("CD_ENVIRONMENT", "demo-local")
    monkeypatch.setenv("CD_COMMIT_SHA", A)
    monkeypatch.setenv("CD_OPERATION", "deploy")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SF_DEPLOY_HOME", str(tmp_path / "data"))
    with pytest.raises(md.DeploymentError, match="outside_checkout"):
        cd.settings()


def test_real_git_source_provenance_and_dirty_rejection(tmp_path):
    def git(*args):
        return cd.command(["git", *args], tmp_path)
    git("init", "--initial-branch=master")
    git("config", "user.name", "Deployment Test")
    git("config", "user.email", "deployment@example.invalid")
    (tmp_path / "docker-compose.yml").write_bytes((cd.CONTROLLER_ROOT / "docker-compose.yml").read_bytes())
    git("add", "docker-compose.yml")
    git("commit", "-m", "fixture")
    candidate = git("rev-parse", "HEAD")
    git("remote", "add", "origin", f"https://github.com/{cd.REPOSITORY}.git")
    git("update-ref", "refs/remotes/origin/master", candidate)
    cd.validate_source(tmp_path, candidate)
    (tmp_path / "dirty.txt").write_text("local change")
    with pytest.raises(md.DeploymentError, match="not_clean"):
        cd.validate_source(tmp_path, candidate)
    (tmp_path / "dirty.txt").unlink()
    git("checkout", "-b", "unpublished")
    (tmp_path / "new.txt").write_text("not in remote history")
    git("add", "new.txt")
    git("commit", "-m", "unpublished")
    with pytest.raises(md.DeploymentError):
        cd.validate_source(tmp_path, git("rev-parse", "HEAD"))


def test_subprocess_isolation_and_safe_errors(tmp_path):
    assert cd.command([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], tmp_path) == "''"
    with pytest.raises(md.DeploymentError, match="deployment_command_failed") as error:
        cd.command([sys.executable, "-c", "import sys; sys.stderr.write('sensitive-value'); sys.exit(1)"], tmp_path)
    assert "sensitive-value" not in str(error.value)


def test_subprocess_timeout_is_bounded(tmp_path):
    with pytest.raises(md.DeploymentError, match="timeout"):
        cd.command([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, timeout=0.1)


@pytest.mark.parametrize("stage,known,expected", [
    ("validate", True, "platform_validation_missing_or_mismatched"),
    ("build", True, "deployment_command_timeout"),
    ("validate", False, "deployment_failed"),
    ("build", False, "deployment_failed"),
])
def test_failure_diagnostics_durable_and_cli_safe(tmp_path, simulated, monkeypatch, capsys,
                                                stage, known, expected):
    events, base = simulated
    complete(tmp_path, A, "deploy", "first")
    complete(tmp_path, B, "deploy", "second")

    def fail(*args):
        if known:
            raise md.DeploymentError(expected)
        raise RuntimeError("password=private-value token=never-persist")

    if stage == "validate":
        monkeypatch.setattr(cd, "validate_evidence", fail)

    class Docker(base):
        def compose(self, *args, **kwargs):
            if stage == "build" and args[0] == "build":
                fail()
            return super().compose(*args, **kwargs)

    original_deploy = cd.deploy
    monkeypatch.setattr(cd, "deploy", lambda *args: original_deploy(
        *args, docker_factory=Docker, health=lambda: None))
    monkeypatch.setattr(cd, "settings", lambda: (tmp_path, C, "deploy"))
    monkeypatch.setenv("CD_ATTEMPT_ID", "failed-attempt")
    monkeypatch.setattr(sys, "argv", ["deploy_local.py", "run", "--source", str(tmp_path / "source")])
    assert cd.main() == 1
    output = capsys.readouterr().out
    assert f"Deployment failed: {expected}\nStage: {stage}\n" in output
    assert "Traceback" not in output

    # Read a fresh journal from disk, including the prior successful deployments.
    state = md.Journal(tmp_path).data
    assert (state["current_sha"], state["previous_sha"]) == (B, A)
    attempt = state["attempts"][-1]
    assert attempt["status"] == "failed"
    assert attempt["reason"] == f"{stage}_failed"
    assert attempt["reason_code"] == expected
    assert attempt["stage"] == stage
    assert (attempt["current_sha"], attempt["previous_sha"]) == (B, A)
    assert attempt["deployed_at"] is None
    assert events.count("up") == 0
    for sensitive in ("private-value", "never-persist", "RuntimeError", "Traceback"):
        assert sensitive not in output
        assert sensitive not in (tmp_path / "deployment.json").read_text()


@pytest.mark.parametrize("args,expected", [
    (["git", "rev-parse", "HEAD"], "git_rev_parse"),
    (["git", "remote", "get-url", "origin"], "git_remote_get_url"),
    (["git", "merge-base", "--is-ancestor", A, B], "git_merge_base"),
    (["git", "status"], "git_status"),
    (["git", "ls-files"], "git_ls_files"),
    (["docker", "context", "inspect"], "docker_context_inspect"),
    (["docker", "info"], "docker_info"),
    (["docker", "inspect", "container"], "docker_inspect"),
    (["docker", "compose", "--env-file", "private-path", "-f", "private-config", "config"], "docker_compose_config"),
    (["docker", "compose", "-p", "project", "-f", "private-config", "ps"], "docker_compose_ps"),
    (["docker", "compose", "-p", "project", "-f", "private-config", "build"], "docker_compose_build"),
    (["docker", "compose", "-p", "project", "-f", "private-config", "up", "-d"], "docker_compose_up"),
    (["docker", "compose", "-f", "build"], None),
    (["unknown", "git", "status"], None),
    (["docker", "compose", "--unknown", "secret", "config"], None),
])
def test_safe_command_operation_allowlist(args, expected):
    assert cd.command_operation(args) == expected


@pytest.mark.parametrize("args,operation,timed_out", [
    (["git", "merge-base", "--is-ancestor", A, B], "git_merge_base", False),
    (["docker", "compose", "--env-file", "secret-path", "config"], "docker_compose_config", False),
    (["docker", "compose", "-f", "secret-path", "build"], "docker_compose_build", True),
])
def test_command_operation_survives_journal_and_cli(tmp_path, simulated, monkeypatch, capsys,
                                                 args, operation, timed_out):
    _, docker = simulated
    complete(tmp_path, A, "deploy", "first")
    complete(tmp_path, B, "deploy", "second")

    class Process:
        returncode, pid = 1, 123
        calls = 0
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def communicate(self, **kwargs):
            self.calls += 1
            if timed_out and self.calls == 1:
                raise subprocess.TimeoutExpired(args, 1, output=b"secret-stdout", stderr=b"secret-stderr")
            return b"secret-stdout", b"secret-stderr"
        def kill(self):
            pass

    monkeypatch.setattr(cd.subprocess, "Popen", Process)
    monkeypatch.setattr(cd.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(cd, "validate_source", lambda *unused: cd.command(args, tmp_path))
    original = cd.deploy
    monkeypatch.setattr(cd, "deploy", lambda *args: original(*args, docker_factory=docker, health=lambda: None))
    monkeypatch.setattr(cd, "settings", lambda: (tmp_path, C, "deploy"))
    monkeypatch.setenv("CD_ATTEMPT_ID", "diagnostic")
    monkeypatch.setattr(sys, "argv", ["deploy_local.py", "run", "--source", str(tmp_path / "source")])
    assert cd.main() == 1
    code = "deployment_command_timeout" if timed_out else "deployment_command_failed"
    output = capsys.readouterr().out
    assert output == f"Deployment failed: {code}\nStage: validate\nCommand operation: {operation}\n"
    state = md.Journal(tmp_path).data
    assert (state["current_sha"], state["previous_sha"]) == (B, A)
    attempt = state["attempts"][-1]
    assert attempt["reason"] == "validate_failed"
    assert attempt["reason_code"] == code
    assert attempt["command_operation"] == operation
    for secret in ("secret-stdout", "secret-stderr", "secret-path"):
        assert secret not in output + (tmp_path / "deployment.json").read_text()


@pytest.mark.parametrize("operation,expected", [("git_status", "git_status"), ("token=private", None)])
def test_explicit_operation_is_allowlisted(tmp_path, operation, expected):
    with pytest.raises(md.DeploymentError) as error:
        cd.command([sys.executable, "-c", "import sys; sys.exit(1)"], tmp_path, operation=operation)
    assert str(error.value) == "deployment_command_failed"
    assert error.value.operation == expected


@pytest.mark.integration
def test_real_compose_generated_config_is_valid(tmp_path):
    import shutil
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is required; no engine used")
    source, home = tmp_path / "source", tmp_path / "persistent"
    source.mkdir()
    (source / "frontend").mkdir()
    shutil.copyfile(cd.CONTROLLER_ROOT / "docker-compose.yml", source / "docker-compose.yml")
    env_file = home / "config" / ".env.production"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("APP_ENV=production\n")
    actual = cd.compose_config(source, home, A)
    generated = home / "compose.json"
    md.atomic_write(generated, actual)
    result = cd.command(["docker", "compose", "-f", str(generated), "config", "--quiet"], source)
    assert result == ""
    assert actual["services"]["backend"]["build"]["context"] == str(source)
    assert actual["services"]["frontend"]["build"]["context"] == str(source / "frontend")
    assert actual["volumes"]["factory_data"]["name"] == cd.VOLUMES["factory_data"]
