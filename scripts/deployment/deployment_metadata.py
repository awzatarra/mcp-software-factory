"""Small atomic deployment journal, independent from the application stores."""

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


COMMAND_OPERATIONS = frozenset({
    "git_rev_parse", "git_remote_get_url", "git_merge_base", "git_status", "git_ls_files",
    "docker_compose_config", "docker_context_inspect", "docker_info", "docker_compose_ps",
    "docker_inspect", "docker_compose_build", "docker_compose_up",
    "ministack_health", "ecr_describe_repositories", "ecr_create_repository",
    "ecr_describe_images", "docker_build", "docker_tag", "docker_push", "docker_pull",
    "docker_compose_stop", "docker_start", "docker_create", "docker_volume",
})


class DeploymentError(Exception):
    """Only fixed, non-sensitive diagnostic codes cross the CLI boundary."""

    def __init__(self, reason_code, *, stage=None, operation=None):
        super().__init__(reason_code)
        self.stage = stage
        self.operation = operation if operation in COMMAND_OPERATIONS else None


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise DeploymentError("invalid_commit_sha")
    return value.lower()


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DeploymentError("invalid_or_missing_local_json") from None


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def deployment_lock(home):
    """OS lock is released even when the runner is terminated."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "deployment.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DeploymentError("deployment_already_running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Journal:
    def __init__(self, home, environment="demo-local"):
        if environment not in ("demo-local", "demo-aws-emulated"):
            raise DeploymentError("unsupported_environment")
        self.environment = environment
        self.path = Path(home) / "deployment.json"
        self.data = read_json(self.path) if self.path.exists() else {
            "environment": environment, "current_sha": None,
            "previous_sha": None, "status": "pending", "attempts": [],
        }
        if (self.data.get("environment") != environment
                or self.data.get("target", environment) != environment
                or not isinstance(self.data.get("attempts"), list)):
            raise DeploymentError("invalid_deployment_metadata")
        if environment == "demo-aws-emulated":
            self.data["target"] = environment
        for key in ("current_sha", "previous_sha"):
            if self.data.get(key) is not None:
                sha(self.data[key])

    def save(self):
        atomic_write(self.path, self.data)

    def begin(self, attempt_id, candidate, operation):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", attempt_id):
            raise DeploymentError("invalid_attempt_id")
        existing = next((a for a in self.data["attempts"]
                         if a["attempt_id"] == attempt_id), None)
        if existing:
            if existing["deployed_sha"] != candidate or existing["operation"] != operation:
                raise DeploymentError("attempt_identity_conflict")
            if existing["status"] in ("healthy", "rolled_back"):
                return None
            raise DeploymentError("attempt_already_recorded_use_new_id")
        # An OS lock has been acquired: any unfinished predecessor was interrupted.
        for previous in self.data["attempts"]:
            if previous["status"] in ("pending", "deploying"):
                previous.update(status="failed", reason="interrupted_attempt", finished_at=now())
        attempt = {
            "environment": self.environment, "deployed_sha": sha(candidate),
            "version": candidate, "deployed_at": None, "status": "pending",
            "current_sha": self.data["current_sha"], "previous_sha": self.data["previous_sha"],
            "attempt_id": attempt_id, "operation": operation, "started_at": now(),
        }
        if self.environment == "demo-aws-emulated":
            attempt["target"] = self.environment
        self.data["attempts"].append(attempt)
        self.data.update(status="pending", latest_attempt_id=attempt_id)
        self.save()
        return attempt

    def deploying(self, attempt):
        attempt["status"] = self.data["status"] = "deploying"
        self.save()

    def finish(self, attempt, success, reason=None, *, reason_code=None, stage=None,
               command_operation=None):
        if attempt["status"] not in ("pending", "deploying"):
            return
        if success:
            candidate = attempt["deployed_sha"]
            if candidate != self.data["current_sha"]:
                self.data["previous_sha"] = self.data["current_sha"]
                self.data["current_sha"] = candidate
            attempt.update(status="rolled_back" if attempt["operation"] == "rollback" else "healthy",
                           deployed_at=now())
            self.data["status"] = "healthy"
        else:
            attempt.update(status="failed", reason=reason)
            if reason_code is not None:
                attempt["reason_code"] = reason_code
            if stage is not None:
                attempt["stage"] = stage
            if command_operation in COMMAND_OPERATIONS:
                attempt["command_operation"] = command_operation
            self.data["status"] = "failed"
        attempt.update(current_sha=self.data["current_sha"], previous_sha=self.data["previous_sha"],
                       finished_at=now())
        self.save()
