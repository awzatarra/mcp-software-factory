"""Opt-in, isolated MiniStack 1.5.8 ECS networking investigation only."""

import argparse
import json
import socket
import threading
import time
import urllib.request
import uuid
from pathlib import Path

try:
    from .validate_ministack_capabilities import IMAGE, docker, inspect
except ImportError:
    from validate_ministack_capabilities import IMAGE, docker, inspect


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def bindings(container):
    return [binding for values in container["NetworkSettings"]["Ports"].values()
            for binding in (values or [])]


def loopback_only(values):
    return bool(values) and all(value.get("HostIp") == "127.0.0.1" for value in values)


def owned_containers(name):
    return docker("ps", "-aq", "--filter",
                  "label=com.amazonaws.ecs.task-definition-family=" + name).split()


def candidate(explicit_hostname, dynamic):
    name = "sf-loop-" + uuid.uuid4().hex[:10]
    network = name + "-net"
    api_port, task_port = free_port(), free_port()
    while api_port == task_port:
        task_port = free_port()
    endpoint = "http://127.0.0.1:" + str(api_port)
    result = {"name": name, "explicit_hostname": explicit_hostname, "dynamic": dynamic,
              "api_port": api_port, "requested_host_port": 0 if dynamic else task_port,
              "bindings": [], "approved": False, "cleanup_errors": []}
    stop = threading.Event()
    rejected = threading.Event()
    guard_errors = []
    guard = None
    created_network = False
    created_container = False

    def api(operation, payload):
        request = urllib.request.Request(endpoint, json.dumps(payload).encode(), headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AmazonEC2ContainerServiceV20141113." + operation,
        })
        with HTTP.open(request, timeout=35) as response:
            return json.load(response)

    def watch_bindings():
        try:
            while not stop.is_set():
                for identifier in owned_containers(name):
                    container = inspect(identifier)
                    values = bindings(container)
                    if values and not loopback_only(values):
                        result["bindings"] = values
                        rejected.set()
                        if container["State"]["Running"]:
                            docker("kill", identifier, timeout=10)
                stop.wait(0.1)
        except Exception:
            guard_errors.append("binding_guard_failed")
            rejected.set()

    try:
        docker("network", "create", "--label", "sf.loopback=" + name, "--opt",
               "com.docker.network.bridge.host_binding_ipv4=127.0.0.1", network)
        created_network = True
        args = ["run", "-d", "--name", name, "--network", network,
                "--label", "sf.loopback=" + name, "-p", f"127.0.0.1:{api_port}:4566",
                "-e", "MINISTACK_HOSTNAME=" + name, "-e", "PERSIST_STATE=0",
                "-v", "/var/run/docker.sock:/var/run/docker.sock"]
        if explicit_hostname:
            args += ["-e", "HOSTNAME=" + name]
        docker(*args, IMAGE)
        created_container = True
        deadline = time.monotonic() + 60
        while True:
            try:
                with HTTP.open(endpoint + "/_ministack/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("health_timeout")
            time.sleep(1)
        result["hostname_seen_by_python"] = docker("exec", name, "python", "-c",
            "import os; print(os.environ.get('HOSTNAME', '<absent>'))")
        api("CreateCluster", {"clusterName": name})
        task = api("RegisterTaskDefinition", {
            "family": name, "networkMode": "bridge",
            "containerDefinitions": [{"name": "probe", "image": "busybox:1.37.0", "essential": True,
                "command": ["sh", "-c", "mkdir -p /tmp/http; echo loopback-probe > /tmp/http/index.html; exec httpd -f -p 8080 -h /tmp/http"],
                "portMappings": [{"containerPort": 8080, "hostPort": 0 if dynamic else task_port}]}],
        })["taskDefinition"]["taskDefinitionArn"]
        guard = threading.Thread(target=watch_bindings, daemon=True)
        guard.start()
        api("CreateService", {"cluster": name, "serviceName": "probe",
                              "taskDefinition": task, "desiredCount": 1})
        if rejected.is_set():
            raise RuntimeError("wildcard_or_guard_failure")
        containers = [inspect(c) for c in owned_containers(name)]
        running = [c for c in containers if c["State"]["Running"]]
        if len(running) != 1:
            raise RuntimeError("expected_one_running_container")
        container = running[0]
        result["bindings"] = bindings(container)
        result["task_networks"] = list(container["NetworkSettings"]["Networks"])
        result["expected_network"] = network
        if not loopback_only(result["bindings"]):
            docker("kill", container["Id"], timeout=10)
            raise RuntimeError("non_loopback_binding")
        port = result["bindings"][0]["HostPort"]
        with HTTP.open("http://127.0.0.1:" + port + "/", timeout=5) as response:
            result["http_ok"] = response.status == 200 and response.read().strip() == b"loopback-probe"
        service = api("DescribeServices", {"cluster": name, "services": ["probe"]})["services"][0]
        result["desired_count"] = service["desiredCount"]
        tasks = api("ListTasks", {"cluster": name})["taskArns"]
        managed = container["Config"]["Labels"]["com.amazonaws.ecs.task-arn"] in tasks
        result["ecs_managed"] = managed
        result["approved"] = bool(result["http_ok"] and managed and service["desiredCount"] == 1
                                  and not rejected.is_set() and loopback_only(bindings(inspect(container["Id"]))))
    except Exception as error:
        result["failure"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        # Stop the controller first so it cannot recreate tasks during cleanup.
        if created_container:
            try:
                docker("stop", "-t", "5", name, timeout=15)
            except Exception:
                result["cleanup_errors"].append("controller_stop_failed")
        stop.set()
        if guard:
            guard.join(timeout=40)
            if guard.is_alive():
                result["cleanup_errors"].append("guard_not_stopped")
        try:
            for identifier in owned_containers(name):
                docker("rm", "-f", identifier)
            if created_container:
                docker("rm", "-f", name)
            if created_network:
                docker("network", "rm", network)
        except Exception:
            result["cleanup_errors"].append("owned_resources_cleanup_failed")
        result["guard_errors"] = guard_errors
        if result["cleanup_errors"] or guard_errors:
            result["approved"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if docker("info", "--format", "{{.OSType}}") != "linux":
        raise SystemExit("linux_daemon_required")
    docker("image", "inspect", "busybox:1.37.0")
    results = []
    for explicit, dynamic in ((False, False), (True, False), (True, True)):
        result = candidate(explicit, dynamic)
        results.append(result)
        print(json.dumps(result), flush=True)
        if result["cleanup_errors"]:
            break
    passed = any(r["approved"] for r in results) and not any(r["cleanup_errors"] for r in results)
    report = {"image": IMAGE, "results": results,
              "conclusion": "MINISTACK_ECS_LOOPBACK_SUPPORTED" if passed else "MINISTACK_ECS_LOOPBACK_UNSUPPORTED"}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(report["conclusion"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
