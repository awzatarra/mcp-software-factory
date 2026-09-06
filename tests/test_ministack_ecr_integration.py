"""Explicit local registry proof, not platform deployment or an ECS test."""

import importlib.util
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/deployment"
for name in ("deployment_metadata", "deploy_local", "ministack_ecr", "deploy_ministack"):
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
cd = sys.modules["deploy_ministack"]
ecr = sys.modules["ministack_ecr"]


@pytest.mark.integration
def test_real_ecr_without_socket_push_pull_and_orderly_restart(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 4566))
    name = "sf-ecr-proof-" + uuid.uuid4().hex[:10]
    volume = name + "-state"
    candidate = uuid.uuid4().hex + "a" * 8
    runtime = cd.Runtime(tmp_path, tmp_path)
    run = runtime.run
    tag = f"sf-demo-aws-backend:{candidate}"
    registry_tag = ecr.REGISTRY + "/" + ecr.REPOSITORIES["backend"] + ":" + candidate
    reference = None
    created = False
    volume_created = False
    try:
        run("docker", "volume", "create", volume, operation="docker_volume")
        volume_created = True
        run("docker", "run", "-d", "--name", name,
            "-p", "127.0.0.1:4566:4566", "-e", "SERVICES=ecr", "-e", "PERSIST_STATE=1",
            "-e", "STATE_DIR=/state", "-v", volume + ":/state", cd.MINISTACK_IMAGE,
            operation="docker_create")
        created = True
        cd.health([(ecr.ENDPOINT + "/_ministack/health", False)], timeout=60)
        item = runtime.inspect(name)
        assert any(m["Destination"] == "/state" and m["Name"] == volume for m in item["Mounts"])
        assert {m["Destination"] for m in item["Mounts"]} <= {
            "/state", "/docker-entrypoint-initaws.d", "/etc/localstack/init"}
        assert not item["HostConfig"]["Privileged"]
        cd.check_bindings(item, {"4566/tcp": 4566})
        client = ecr.ECR()
        client.ensure("backend")
        client.ensure("backend")
        # Unique synthetic layer, no platform configuration or credentials.
        (tmp_path / "Dockerfile").write_text(
            "FROM busybox:1.37.0\nLABEL org.opencontainers.image.revision=" + candidate
            + "\nRUN echo " + candidate + " > /capability-marker\n", encoding="utf-8")
        run("docker", "build", "-t", tag, str(tmp_path), operation="docker_build", timeout=180)
        value = runtime.push("backend", candidate, client)
        reference = ecr.image_reference("backend", value)
        run("docker", "image", "rm", tag, registry_tag, operation="docker_tag")
        runtime.pull("backend", candidate, value)
        run("docker", "image", "rm", reference, operation="docker_tag")
        run("docker", "stop", "-t", "30", name, operation="docker_compose_stop")
        run("docker", "start", name, operation="docker_start")
        cd.health([(ecr.ENDPOINT + "/_ministack/health", False)], timeout=60)
        assert client.find("backend", candidate) == value
        runtime.pull("backend", candidate, value)
        assert run("docker", "run", "--rm", reference, "cat", "/capability-marker") == candidate
        print(json.dumps({"registry_without_socket": True, "push_pull": True,
                          "orderly_restart_pull": True, "digest": value}))
    finally:
        if created:
            run("docker", "rm", "-f", "-v", name, operation="docker_compose_stop")
        if volume_created:
            run("docker", "volume", "rm", volume, operation="docker_volume")
        # Only this probe's tags/digest; absence is harmless after earlier cleanup.
        for image in (tag, registry_tag, reference):
            if image:
                try:
                    run("docker", "image", "rm", image, operation="docker_tag")
                except cd.DeploymentError:
                    pass
