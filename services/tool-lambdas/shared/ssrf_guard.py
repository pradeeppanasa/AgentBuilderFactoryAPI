"""SSRF protection for outbound HTTP tool calls (Sprint 3 Phase 5 — CLAUDE.md
Section 64.5, S-09, R66).

R66 in one line: validate the IP a request will actually connect to, not
just the URL string it started with — a hostname can resolve to a
private/metadata address at connection time even when the URL text looks
innocuous (DNS rebinding), and an allowed domain can redirect somewhere
unapproved (open redirect). Both are checked here, at connection time.

Where this is meant to be imported from (Section 59 Gap 2 / DEP-INF-05):
this module has no real caller yet. `tools.tf.j2` still generates each
per-tool Lambda with `filename = "placeholder.zip"` — there is no actual
Lambda handler code in this codebase today that makes an outbound HTTP
call at all, so there is nothing for SSRFSafeSession to be wired into
until that separate, already-tracked gap is closed. This module is built
and fully tested now so it's ready the moment a real tool handler exists;
see this file's own tests for the acceptance criteria it satisfies in
isolation.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Any

import requests

BLOCKED_CIDRS = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/cloud metadata
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fd00::/8"),  # IPv6 ULA
]


def _is_ip_blocked(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable → block (fail closed, R39)

    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) is a well-known SSRF
    # filter bypass: the string clearly reads as IPv6 and every check
    # above would pass, but the OS still connects to the embedded IPv4
    # address. Check both forms.
    if (
        isinstance(addr, ipaddress.IPv6Address)
        and addr.ipv4_mapped is not None
        and any(addr.ipv4_mapped in net for net in BLOCKED_CIDRS)
    ):
        return True

    return any(addr in net for net in BLOCKED_CIDRS)


def validate_url_safe(url: str, allowed_domains: list[str]) -> None:
    """Validate a URL and every IP its hostname resolves to. Call before
    every outbound HTTP request — including before following a redirect
    (see follow_redirect_safely below). Raises ValueError on any failure;
    never returns a bool a caller could accidentally ignore.
    """
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS allowed for tool outbound calls. Got: {parsed.scheme!r}")

    hostname = parsed.hostname or ""
    if not any(hostname == d or hostname.endswith("." + d) for d in allowed_domains):
        raise ValueError(f"Domain {hostname!r} not in agent's allowed_domains list")

    # Resolve NOW and check every returned IP — this is what actually
    # catches DNS rebinding (checking the URL string alone cannot; a
    # hostname that resolves to a public IP at validation time can be
    # re-pointed at a private IP by the time the connection is opened,
    # which is why this module also disables redirects rather than
    # trusting a second DNS lookup to be consistent with the first).
    try:
        resolved = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {hostname!r}: {exc}") from exc

    if not resolved:
        raise ValueError(f"DNS resolution for {hostname!r} returned no addresses")

    for result in resolved:
        ip = result[4][0]
        if _is_ip_blocked(ip):
            raise ValueError(f"Resolved IP {ip} for {hostname!r} is in a blocked range")


class SSRFSafeSession(requests.Session):
    """A requests.Session that validates every URL (and its resolved IPs)
    before connecting, and never follows a redirect automatically — an
    allow-listed domain redirecting to a private IP is exactly the kind
    of thing R66 exists to stop, and once a redirect starts following
    itself, this session is no longer the thing deciding where the
    connection actually goes."""

    def __init__(self, allowed_domains: list[str]) -> None:
        super().__init__()
        self._allowed_domains = allowed_domains
        self.max_redirects = 0  # belt-and-suspenders alongside allow_redirects=False below

    def request(  # type: ignore[override]  # requests.Session.request declares
        # a dozen specific keyword params; **kwargs: Any forwards all of them
        # to super().request() unchanged, but mypy can't verify a catch-all
        # matches each one structurally.
        self,
        method: str,
        url: str | bytes,
        **kwargs: Any,
    ) -> requests.Response:
        validate_url_safe(str(url), self._allowed_domains)
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def follow_redirect_safely(
    session: SSRFSafeSession, response: requests.Response, **kwargs: Any
) -> requests.Response:
    """For the rare tool that legitimately needs to follow a redirect
    (Section 64.5's own "critical implementation note #3"): re-validates
    the Location header — the same validate_url_safe check every other
    request goes through — before issuing the follow-up request. Never
    call requests' own automatic redirect-following instead of this; that
    is exactly the bypass R66 disables redirects to prevent.
    """
    if not response.is_redirect:
        raise ValueError("Response is not a redirect — nothing to follow")

    # requests.Response.is_redirect already requires a Location header to
    # be true, so this is unreachable through a real Response — kept as
    # defence-in-depth against a hand-constructed one rather than trusting
    # that invariant to hold forever.
    location = response.headers.get("Location")
    if not location:
        raise ValueError("Redirect response has no Location header")

    next_url = urllib.parse.urljoin(response.url, location)
    validate_url_safe(next_url, session._allowed_domains)  # noqa: SLF001 - same module
    return session.request(response.request.method or "GET", next_url, **kwargs)
