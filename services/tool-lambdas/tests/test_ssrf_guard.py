"""Tests for the Sprint 3 Phase 5 SSRF guard (CLAUDE.md Section 64.5, S-09,
R66). DNS is mocked throughout — these tests must never depend on, or
perform, a real network lookup or connection.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
import requests

from shared.ssrf_guard import (
    SSRFSafeSession,
    follow_redirect_safely,
    validate_url_safe,
)


def _addrinfo(*ips: str) -> list[tuple[Any, ...]]:
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        if ":" not in ip
        else (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))
        for ip in ips
    ]


def _mock_dns(monkeypatch: pytest.MonkeyPatch, resolved: dict[str, list[str]]) -> None:
    def _fake_getaddrinfo(hostname: str, port: Any) -> list[tuple[Any, ...]]:
        if hostname not in resolved:
            raise socket.gaierror(f"no mock DNS entry for {hostname!r}")
        return _addrinfo(*resolved[hostname])

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


# ── validate_url_safe ────────────────────────────────────────────────────


def test_rejects_non_https_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    with pytest.raises(ValueError, match="Only HTTPS"):
        validate_url_safe("http://api.jira.com/", ["api.jira.com"])


def test_rejects_domain_not_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"evil.example.com": ["203.0.113.10"]})

    with pytest.raises(ValueError, match="not in agent's allowed_domains"):
        validate_url_safe("https://evil.example.com/", ["api.jira.com"])


def test_allows_exact_domain_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    validate_url_safe("https://api.jira.com/rest/api/3", ["api.jira.com"])  # no raise


def test_allows_subdomain_of_allowlisted_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"eu.api.jira.com": ["203.0.113.10"]})

    validate_url_safe("https://eu.api.jira.com/", ["api.jira.com"])  # no raise


def test_does_not_allow_lookalike_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """'notapi.jira.com' must not match an allowlist entry of 'api.jira.com'
    via a naive substring/endswith-without-dot check."""
    _mock_dns(monkeypatch, {"notapi.jira.com": ["203.0.113.10"]})

    with pytest.raises(ValueError, match="not in agent's allowed_domains"):
        validate_url_safe("https://notapi.jira.com/", ["api.jira.com"])


def test_request_to_aws_metadata_ip_raises_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["169.254.169.254"]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_request_to_rfc1918_10_range_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["10.0.0.1"]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


@pytest.mark.parametrize(
    "ip",
    [
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "127.0.0.1",
        "::1",
    ],
)
def test_request_to_each_blocked_range_raises(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": [ip]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_ipv6_ula_range_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["fd00::1"]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_ipv4_mapped_ipv6_bypass_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-known SSRF filter bypass: ::ffff:169.254.169.254 reads as
    IPv6 (missing from a naive IPv4-only blocklist check) but the OS
    still connects to the embedded IPv4 metadata address."""
    _mock_dns(monkeypatch, {"api.jira.com": ["::ffff:169.254.169.254"]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_any_bad_ip_among_multiple_resolved_addresses_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS returning a mix of a public IP and a private IP must block —
    an HTTP client is free to pick any resolved address to connect to."""
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10", "10.0.0.5"]})

    with pytest.raises(ValueError, match="blocked range"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_public_ip_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    validate_url_safe("https://api.jira.com/", ["api.jira.com"])  # no raise


def test_dns_resolution_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_dns(monkeypatch, {})  # nothing resolves

    with pytest.raises(ValueError, match="DNS resolution failed"):
        validate_url_safe("https://api.jira.com/", ["api.jira.com"])


def test_empty_allowed_domains_denies_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default = empty list = deny all outbound HTTP tool calls."""
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    with pytest.raises(ValueError, match="not in agent's allowed_domains"):
        validate_url_safe("https://api.jira.com/", [])


# ── SSRFSafeSession ──────────────────────────────────────────────────────


def test_session_blocks_metadata_ip_before_any_request_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["169.254.169.254"]})
    send_calls: list[Any] = []
    monkeypatch.setattr(requests.Session, "send", lambda self, *a, **kw: send_calls.append(a))

    session = SSRFSafeSession(["api.jira.com"])
    with pytest.raises(ValueError, match="blocked range"):
        session.post("https://api.jira.com/", json={})

    assert send_calls == []  # never reached the transport layer


def test_session_succeeds_for_approved_domain_resolving_to_public_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    fake_response = requests.Response()
    fake_response.status_code = 200

    captured_kwargs: dict[str, Any] = {}

    def _fake_send(self: requests.Session, prepared: Any, **kwargs: Any) -> requests.Response:
        captured_kwargs.update(kwargs)
        return fake_response

    monkeypatch.setattr(requests.Session, "send", _fake_send)

    session = SSRFSafeSession(["api.jira.com"])
    response = session.post("https://api.jira.com/rest/api/3/issue", json={"x": 1})

    assert response is fake_response
    assert captured_kwargs["allow_redirects"] is False


def test_session_forces_allow_redirects_false_even_if_caller_passes_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})
    captured_kwargs: dict[str, Any] = {}

    def _fake_send(self: requests.Session, prepared: Any, **kwargs: Any) -> requests.Response:
        captured_kwargs.update(kwargs)
        resp = requests.Response()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(requests.Session, "send", _fake_send)

    session = SSRFSafeSession(["api.jira.com"])
    session.get("https://api.jira.com/", allow_redirects=True)

    assert captured_kwargs["allow_redirects"] is False


def test_session_max_redirects_is_zero() -> None:
    session = SSRFSafeSession(["api.jira.com"])
    assert session.max_redirects == 0


# ── follow_redirect_safely ───────────────────────────────────────────────


def _redirect_response(location: str, method: str = "GET") -> requests.Response:
    resp = requests.Response()
    resp.status_code = 302
    resp.headers["Location"] = location
    resp.url = "https://api.jira.com/old-path"
    resp.request = requests.PreparedRequest()
    resp.request.method = method
    return resp


def test_follow_redirect_safely_rejects_redirect_to_blocked_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["169.254.169.254"]})
    session = SSRFSafeSession(["api.jira.com"])
    response = _redirect_response("https://api.jira.com/new-path")

    with pytest.raises(ValueError, match="blocked range"):
        follow_redirect_safely(session, response)


def test_follow_redirect_safely_rejects_redirect_outside_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"evil.example.com": ["203.0.113.10"]})
    session = SSRFSafeSession(["api.jira.com"])
    response = _redirect_response("https://evil.example.com/steal")

    with pytest.raises(ValueError, match="not in agent's allowed_domains"):
        follow_redirect_safely(session, response)


def test_follow_redirect_safely_raises_when_not_a_redirect() -> None:
    session = SSRFSafeSession(["api.jira.com"])
    resp = requests.Response()
    resp.status_code = 200

    with pytest.raises(ValueError, match="not a redirect"):
        follow_redirect_safely(session, resp)


def test_follow_redirect_safely_issues_request_to_validated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, {"api.jira.com": ["203.0.113.10"]})

    captured: dict[str, Any] = {}

    def _fake_send(self: requests.Session, prepared: Any, **kwargs: Any) -> requests.Response:
        captured["url"] = prepared.url
        resp = requests.Response()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(requests.Session, "send", _fake_send)

    session = SSRFSafeSession(["api.jira.com"])
    response = _redirect_response("https://api.jira.com/new-path", method="GET")

    result = follow_redirect_safely(session, response)

    assert result.status_code == 200
    assert captured["url"] == "https://api.jira.com/new-path"
