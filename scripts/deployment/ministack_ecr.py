"""Local-only ECR protocol client. No AWS profile or credential discovery."""

import json
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.request

from deployment_metadata import DeploymentError

ENDPOINT = "http://127.0.0.1:4566"
REGISTRY = "localhost:4566"
REPOSITORIES = {s: "mcp-software-factory-demo-aws/" + s for s in ("backend", "frontend")}


def isolated_environment(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("AWS_", "COMPOSE_", "SF_"))}
    env.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test",
               AWS_DEFAULT_REGION="us-east-1", AWS_EC2_METADATA_DISABLED="true")
    env.update(extra or {})
    return env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise DeploymentError("non_local_aws_endpoint")


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise DeploymentError("invalid_image_digest")
    return value


def image_reference(service, value):
    return REGISTRY + "/" + REPOSITORIES[service] + "@" + digest(value)


def validate_reference(service, value):
    prefix = REGISTRY + "/" + REPOSITORIES[service] + "@"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise DeploymentError("non_local_registry")
    digest(value[len(prefix):])
    return value


def validate_registry_host():
    try:
        addresses = socket.getaddrinfo("localhost", 4566, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_loopback for item in addresses):
            raise ValueError
    except (OSError, ValueError):
        raise DeploymentError("non_local_registry") from None


class ECR:
    def __init__(self, endpoint=ENDPOINT, opener=None):
        if endpoint != ENDPOINT:
            raise DeploymentError("non_local_aws_endpoint")
        self.endpoint = endpoint
        self.opener = opener or local_opener()

    def request(self, operation, payload, missing=None):
        labels = {"DescribeRepositories": "ecr_describe_repositories",
                  "CreateRepository": "ecr_create_repository", "DescribeImages": "ecr_describe_images"}
        if operation not in labels:
            raise DeploymentError("unsupported_ecr_operation")
        request = urllib.request.Request(self.endpoint, json.dumps(payload).encode(), headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AmazonEC2ContainerRegistry_V20150921." + operation,
        })
        # The pinned emulator supports unsigned local JSON requests. AWS credentials
        # are never loaded or sent, and no SDK can fall back to a real endpoint.
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.geturl() != self.endpoint:
                    raise DeploymentError("non_local_aws_endpoint")
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read(8192))
                code = str(body.get("__type", body.get("code", ""))).split("#")[-1]
            except (ValueError, AttributeError):
                code = ""
            if 300 <= error.code < 400:
                raise DeploymentError("non_local_aws_endpoint") from None
            if missing and code == missing:
                return None
            raise DeploymentError("ecr_operation_failed", operation=labels[operation]) from None
        except (OSError, ValueError):
            raise DeploymentError("ecr_operation_failed", operation=labels[operation]) from None

    def ensure(self, service):
        name = REPOSITORIES[service]
        data = self.request("DescribeRepositories", {"repositoryNames": [name]}, "RepositoryNotFoundException")
        if data is None:
            data = {"repositories": [self.request("CreateRepository", {
                "repositoryName": name, "imageTagMutability": "IMMUTABLE",
            })["repository"]]}
        repositories = data.get("repositories", [])
        if len(repositories) != 1 or repositories[0].get("repositoryName") != name:
            raise DeploymentError("ecr_repository_mismatch")
        # AWS-shaped repositoryUri returned by the emulator is metadata only;
        # all Docker traffic uses our fixed local registry, never this URI.
        return name

    def find(self, service, candidate):
        data = self.request("DescribeImages", {"repositoryName": REPOSITORIES[service],
                            "imageIds": [{"imageTag": candidate}]}, "ImageNotFoundException")
        if data is None or not data.get("imageDetails"):
            return None
        details = data["imageDetails"]
        if len(details) != 1 or candidate not in details[0].get("imageTags", []):
            raise DeploymentError("ecr_image_mismatch")
        return digest(details[0].get("imageDigest"))
