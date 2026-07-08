"""
Shared SSRF guard for executors that make outbound HTTP requests to
task-supplied URLs (e.g. rest_executor, apflow_api_executor).
"""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

from apflow.core.execution.errors import ValidationError

# Default cap on how long DNS resolution may take, so a slow/black-holed
# resolver for a task-supplied hostname cannot make the outbound request run far
# past the caller's own request timeout (and hold a shared thread-pool worker).
DEFAULT_RESOLVE_TIMEOUT_SECONDS = 5.0


async def validate_url_not_private(
    url: str, executor_id: str, resolve_timeout: float = DEFAULT_RESOLVE_TIMEOUT_SECONDS
) -> None:
    """Validate that a URL does not target private or internal network addresses.

    Resolves the hostname to IP addresses and checks against private, loopback,
    link-local, and reserved ranges to prevent SSRF attacks.

    Can be bypassed by setting env var APFLOW_REST_ALLOW_PRIVATE_URLS=1.

    Note (residual, by design): this validates the resolved IPs, but the caller's
    HTTP client re-resolves DNS independently at connect time, so a DNS-rebinding
    attacker who controls the hostname's authoritative DNS (serving a public IP to
    this check and a private IP to the connection) is not fully blocked here. The
    literal-private-IP and redirect-to-private vectors ARE blocked; close the
    rebinding residual at the network layer with an egress policy that denies
    outbound traffic to internal ranges.

    Args:
        url: The outbound request URL to validate.
        executor_id: Identifier of the calling executor, for error context.
        resolve_timeout: Max seconds to spend resolving the hostname.

    Raises:
        ValidationError: If the URL targets a private/reserved address, cannot be
            resolved, or resolution exceeds ``resolve_timeout``.
    """
    if os.environ.get("APFLOW_REST_ALLOW_PRIVATE_URLS") == "1":
        return

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValidationError(f"[{executor_id}] URL has no hostname: {url}")

    # socket.getaddrinfo() is a blocking call; running it directly here would
    # stall the entire shared event loop (all other concurrently-running
    # tasks) for as long as DNS resolution takes. Offload it to a thread AND
    # bound it, so a slow/black-holed resolver can't overrun the request budget.
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, hostname, None),
            timeout=resolve_timeout,
        )
    except asyncio.TimeoutError:
        raise ValidationError(f"[{executor_id}] Timed out resolving hostname: {hostname}")
    except socket.gaierror:
        raise ValidationError(f"[{executor_id}] Cannot resolve hostname: {hostname}")

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValidationError(
                f"[{executor_id}] URL targets a private/reserved address: {hostname} -> {ip_str}"
            )
