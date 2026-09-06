import io
import subprocess

import pytest

from scripts.deployment import validate_ministack_capabilities as probe


def test_docker_handles_are_isolated(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b"linux\n", b"")

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe.docker("info") == "linux"
    args, options = calls[0]
    assert args == ["docker", "info"]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == options["stderr"] == subprocess.PIPE
    assert options["shell"] is False
    assert options["timeout"] == 120


def test_docker_failure_does_not_expose_output(monkeypatch):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, b"private-output", b"private-error"))
    with pytest.raises(RuntimeError, match="^docker_operation_failed:push$"):
        probe.docker("push", "localhost:4566/probe")


@pytest.mark.parametrize("service,target", [
    ("ecr", "AmazonEC2ContainerRegistry_V20150921"),
    ("ecs", "AmazonEC2ContainerServiceV20141113"),
])
def test_api_uses_local_endpoint_without_credentials(monkeypatch, service, target):
    def open_request(request, timeout):
        assert request.full_url == "http://127.0.0.1:4566"
        assert request.get_header("X-amz-target") == target + ".Describe"
        assert not request.has_header("Authorization")
        assert timeout == 40
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(probe.HTTP, "open", open_request)
    assert probe.api(service, "Describe", {}) == {"ok": True}
