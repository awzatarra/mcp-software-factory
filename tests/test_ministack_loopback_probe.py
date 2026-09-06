import io

import pytest

from scripts.deployment import validate_ministack_loopback as probe


@pytest.mark.parametrize("values,expected", [
    ([], False),
    ([{"HostIp": "127.0.0.1", "HostPort": "12345"}], True),
    ([{"HostIp": "0.0.0.0", "HostPort": "12345"}], False),
    ([{"HostIp": "::", "HostPort": "12345"}], False),
    ([{"HostIp": "::1", "HostPort": "12345"}], False),
    ([{"HostIp": "127.0.0.1"}, {"HostIp": "0.0.0.0"}], False),
    ([{"HostPort": "12345"}], False),
])
def test_exclusive_ipv4_loopback_required(values, expected):
    assert probe.loopback_only(values) is expected


def test_all_ports_are_checked():
    values = probe.bindings({"NetworkSettings": {"Ports": {
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}],
        "9090/tcp": [{"HostIp": "::", "HostPort": "23456"}],
        "1234/udp": None,
    }}})
    assert len(values) == 2
    assert not probe.loopback_only(values)


def test_unpublished_awsvpc_is_not_success():
    assert not probe.loopback_only(probe.bindings({"NetworkSettings": {"Ports": {}}}))


def test_api_failure_still_cleans_only_owned_resources(monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append(args)
        return "" if args[0] == "ps" else "synthetic"

    class Health(io.BytesIO):
        status = 200

    def fake_open(request, timeout):
        if isinstance(request, str):
            return Health(b'{}')
        raise RuntimeError("synthetic_api_failure")

    monkeypatch.setattr(probe, "docker", fake_docker)
    monkeypatch.setattr(probe.HTTP, "open", fake_open)
    result = probe.candidate(True, False)
    assert result["approved"] is False
    assert result["failure"] == "synthetic_api_failure"
    assert result["cleanup_errors"] == []
    name = result["name"]
    assert ("stop", "-t", "5", name) in calls
    assert ("rm", "-f", name) in calls
    assert ("network", "rm", name + "-net") in calls
    assert not any("prune" in call for call in calls)


def test_redirects_are_disabled():
    assert probe.NoRedirect().redirect_request(None, None, None, None, None, None) is None
