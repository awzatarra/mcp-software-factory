from pathlib import Path
import subprocess

import pytest

from scripts.audit_public_repository import audit, forbidden_path, scan_text


@pytest.mark.parametrize("name", [".env", "frontend/.env.local", "data/store.sqlite", "workspace/demo/main.py", "logs/run.log", "a.sqlite3-wal", "demo.webm"])
def test_runtime_paths_rejected(name):
    assert forbidden_path(name)


@pytest.mark.parametrize("name", [".env.example", ".env.production.example", "frontend/.env.example", "workspace/.gitkeep", "tests/test_api.py"])
def test_public_paths_allowed(name):
    assert not forbidden_path(name)


def test_redacted_findings_only():
    token = "ghp_" + "a" * 36
    findings = scan_text(("token = " + token).encode())
    assert findings == [{"rule": "provider_token", "line": 1}]
    assert token not in str(findings)


def test_deployment_workflows_are_manual_and_least_privilege():
    import yaml
    workflows = list(Path(".github/workflows").glob("*.yml"))
    assert workflows
    for path in workflows:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(value.get("on", value.get(True, {}))) == {"workflow_dispatch"}
        assert value["permissions"] == {"contents": "read"}
        for job in value["jobs"].values():
            assert job.get("permissions", value["permissions"]) == {"contents": "read"}
            for step in job["steps"]:
                if "uses" not in step:
                    continue
                action, sha = step["uses"].rsplit("@", 1)
                assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
                if action == "actions/checkout":
                    assert step["with"]["persist-credentials"] is False


def test_untracked_candidate_secret_is_not_missed(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "new.txt").write_text("ghp_" + "c" * 36)
    assert not audit(tmp_path)["findings"]
    assert audit(tmp_path, include_untracked=True)["findings"][0]["rule"] == "provider_token"


def test_ignore_rules_preserve_examples_and_block_private_artifacts(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_bytes(Path(".gitignore").read_bytes())
    private = [".env", ".env.staging", "frontend/.env.local", "a.sqlite3", "a.sqlite3-wal",
               "logs/run.log", "workspace/demo/main.py", "artifacts/export.json", ".coverage", "snapshots/run.json"]
    public = [".env.example", ".env.production.example", "frontend/.env.example", "README.md"]
    for name in private + public:
        result = subprocess.run(["git", "-c", f"safe.directory={tmp_path.as_posix()}",
                                 "check-ignore", "--no-index", "-q", name], cwd=tmp_path)
        assert result.returncode == (0 if name in private else 1), name


def test_history_finds_deleted_secret_without_exposing_it(tmp_path: Path):
    def git(*args):
        return subprocess.run(["git", "-c", f"safe.directory={tmp_path.as_posix()}", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Audit Fixture")
    token = "ghp_" + "b" * 36
    (tmp_path / "fixture.txt").write_text(token)
    git("add", "fixture.txt")
    git("commit", "-m", "fixture")
    (tmp_path / "fixture.txt").write_text("removed")
    git("commit", "-am", "remove")
    assert audit(tmp_path)["findings"] == []
    result = audit(tmp_path, history=True)
    assert any(f["rule"] == "provider_token" and f["scope"] == "history" for f in result["findings"])
    assert token not in str(result)
