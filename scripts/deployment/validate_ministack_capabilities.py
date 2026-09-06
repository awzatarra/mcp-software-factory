"""Opt-in local capability probe; not a CD controller or application deployment."""

import argparse
import json
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path


IMAGE = "ministackorg/ministack@sha256:d865b1e43b0b1a7e6f246e1a46fb8a0a2fa04e2a94c64740c8ff454748040726"
ENDPOINT = "http://127.0.0.1:4566"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def docker(*args, timeout=120):
    result = subprocess.run(
        ["docker", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("docker_operation_failed:" + args[0])
    return result.stdout.decode("utf-8", errors="replace").strip()


def api(service, operation, payload):
    target = "AmazonEC2ContainerRegistry_V20150921" if service == "ecr" else "AmazonEC2ContainerServiceV20141113"
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": target + "." + operation},
    )
    # MiniStack accepts unsigned local requests; no SDK credential chain is used.
    with HTTP.open(request, timeout=40) as response:
        return json.load(response)


def healthy():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with HTTP.open(ENDPOINT + "/_ministack/health", timeout=3) as response:
                if response.status == 200:
                    return json.load(response)
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise RuntimeError("ministack_health_timeout")


def inspect(identifier):
    return json.loads(docker("inspect", identifier))[0]


def task_containers(cluster):
    return docker("ps", "-aq", "--filter", "label=com.amazonaws.ecs.cluster=" + cluster).split()


def run(report_path):
    name = "sf-cap-" + uuid.uuid4().hex[:10]
    network = name + "-net"
    volumes = [name + "-state", name + "-workspace", name + "-data"]
    repository = name + "/probe"
    image = "localhost:4566/" + repository + ":capability"
    report = {"probe": name, "ministack_image": IMAGE, "checks": {}, "volumes": volumes,
              "scope": "capability-only", "credentials": "unsigned-local-no-credential-chain"}
    checks = report["checks"]
    cluster = None
    started = False
    try:
        if docker("info", "--format", "{{.OSType}}") != "linux":
            raise RuntimeError("linux_daemon_required")
        for port in (4566, 18081):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        docker("network", "create", "--label", "sf.capability=" + name,
               "--opt", "com.docker.network.bridge.host_binding_ipv4=127.0.0.1", network)
        for volume in volumes:
            docker("volume", "create", "--label", "sf.capability=" + name, volume)
        docker("run", "-d", "--name", name, "--network", network,
               "--label", "sf.capability=" + name, "-p", "127.0.0.1:4566:4566",
               "-e", "PERSIST_STATE=1", "-e", "STATE_DIR=/state",
               "-e", "MINISTACK_HOSTNAME=" + name, "-e", "MINISTACK_HOST=localhost",
               "-v", volumes[0] + ":/state", "-v", "/var/run/docker.sock:/var/run/docker.sock", IMAGE)
        started = True
        checks["health"] = bool(healthy())
        checks["docker_socket"] = docker("exec", name, "python", "-c",
            "import docker; print(docker.from_env().ping())") == "True"
        api("ecr", "CreateRepository", {"repositoryName": repository})
        checks["ecr_create_repository"] = True
        docker("pull", "busybox:1.37.0")
        docker("tag", "busybox:1.37.0", image)
        docker("push", image)
        checks["ecr_push"] = True
        images = api("ecr", "DescribeImages", {"repositoryName": repository})["imageDetails"]
        report["image_digest"] = images[0]["imageDigest"]
        checks["ecr_describe_image"] = bool(images)
        docker("image", "rm", image)
        docker("pull", image)
        checks["ecr_pull"] = True
        cluster = api("ecs", "CreateCluster", {"clusterName": name})["cluster"]["clusterArn"]
        checks["ecs_create_cluster"] = True
        task = api("ecs", "RegisterTaskDefinition", {
            "family": name, "networkMode": "bridge",
            "volumes": [{"name": "workspace", "host": {"sourcePath": volumes[1]}},
                        {"name": "data", "host": {"sourcePath": volumes[2]}}],
            "containerDefinitions": [{"name": "probe", "image": image, "essential": True,
                "command": ["sh", "-c", "mkdir -p /app/workspace /app/data; "
                    "test -f /app/workspace/sentinel || echo capability-workspace > /app/workspace/sentinel; "
                    "test -f /app/data/sentinel || echo capability-data > /app/data/sentinel; "
                    "exec httpd -f -p 8080 -h /app/workspace"],
                # MiniStack 1.5.8 published wildcards in the initial probe.
                # Keep subsequent storage/restart probes entirely unpublished.
                "portMappings": [],
                "mountPoints": [{"sourceVolume": "workspace", "containerPath": "/app/workspace"},
                                {"sourceVolume": "data", "containerPath": "/app/data"}]}],
        })["taskDefinition"]["taskDefinitionArn"]
        checks["ecs_register_task"] = True
        api("ecs", "CreateService", {"cluster": name, "serviceName": "probe", "taskDefinition": task, "desiredCount": 1})
        checks["ecs_create_service"] = True
        time.sleep(3)
        containers = task_containers(cluster)
        running = [identifier for identifier in containers if inspect(identifier)["State"]["Running"]]
        checks["ecs_real_container"] = len(running) == 1
        if not running:
            raise RuntimeError("ecs_no_running_container")
        current = inspect(running[0])
        bindings = current["NetworkSettings"]["Ports"].get("8080/tcp") or []
        report["bindings"] = bindings
        checks["no_published_task_ports"] = not bindings
        report["loopback_publication"] = "blocked_by_prior_wildcard_binding_probe"
        if bindings:
            raise RuntimeError("non_loopback_binding")
        report["mounts"] = [{"type": m["Type"], "name": m.get("Name"), "destination": m["Destination"], "rw": m["RW"]} for m in current["Mounts"]]
        before = docker("exec", running[0], "sha256sum", "/app/workspace/sentinel", "/app/data/sentinel")
        checks["storage_read_write"] = "capability-data" == docker("exec", running[0], "cat", "/app/data/sentinel")
        docker("stop", "-t", "30", name)
        report["containers_running_while_ministack_stopped"] = sum(inspect(c)["State"]["Running"] for c in task_containers(cluster))
        docker("start", name)
        healthy()
        time.sleep(6)
        checks["restart_ecr_metadata"] = report["image_digest"] in [d["imageDigest"] for d in api("ecr", "DescribeImages", {"repositoryName": repository})["imageDetails"]]
        docker("image", "rm", image)
        docker("pull", image)
        checks["restart_ecr_pull"] = True
        checks["restart_ecs_cluster"] = bool(api("ecs", "DescribeClusters", {"clusters": [name]})["clusters"])
        checks["restart_ecs_task_definition"] = bool(api("ecs", "DescribeTaskDefinition", {"taskDefinition": task})["taskDefinition"])
        service = api("ecs", "DescribeServices", {"cluster": name, "services": ["probe"]})["services"][0]
        checks["restart_ecs_service"] = service["desiredCount"] == 1
        running = [c for c in task_containers(cluster) if inspect(c)["State"]["Running"]]
        checks["restart_single_container"] = len(running) == 1
        checks["restart_storage"] = bool(running) and before == docker("exec", running[0], "sha256sum", "/app/workspace/sentinel", "/app/data/sentinel")
    except Exception as error:
        report["failure"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        cleanup_errors = []
        if started:
            try:
                docker("stop", "-t", "30", name)
            except Exception:
                cleanup_errors.append("ministack_stop_failed")
        if cluster:
            try:
                for identifier in task_containers(cluster):
                    if inspect(identifier)["State"]["Running"]:
                        docker("stop", "-t", "5", identifier)
            except Exception:
                cleanup_errors.append("task_stop_failed")
        report["cleanup_errors"] = cleanup_errors
        report["retained_stopped_container"] = name if started else None
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    return int(bool(report.get("failure") or cleanup_errors or not all(checks.values())
                    or report.get("loopback_publication")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    raise SystemExit(run(options.report))
