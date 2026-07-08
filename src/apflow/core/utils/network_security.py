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


async def validate_url_not_private(url: str, executor_id: str) -> None:
    """Validate that a URL does not target private or internal network addresses.

    Resolves the hostname to IP addresses and checks against private, loopback,
    link-local, and reserved ranges to prevent SSRF attacks.

    Can be bypassed by setting env var APFLOW_REST_ALLOW_PRIVATE_URLS=1.

    Args:
        url: The outbound request URL to validate.
        executor_id: Identifier of the calling executor, for error context.

    Raises:
        ValidationError: If the URL targets a private/reserved address.
    """
    if os.environ.get("APFLOW_REST_ALLOW_PRIVATE_URLS") == "1":
        return

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValidationError(f"[{executor_id}] URL has no hostname: {url}")

    # socket.getaddrinfo() is a blocking call; running it directly here would
    # stall the entire shared event loop (all other concurrently-running
    # tasks) for as long as DNS resolution takes. Offload it to a thread.
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
    except socket.gaierror:
        raise ValidationError(f"[{executor_id}] Cannot resolve hostname: {hostname}")

    for addr_info in addr_infos:
        ip_str = addr_info[4][0]
        ip = ipaddress.ip_address(ip_str)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValidationError(
                f"[{executor_id}] URL targets a private/reserved address: {hostname} -> {ip_str}"
            )
