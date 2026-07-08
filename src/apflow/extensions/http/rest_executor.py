"""
REST API Executor for calling REST API endpoints.

This executor is designed to make HTTP requests to third-party REST API services, webhooks, and HTTP-based endpoints.
It supports authentication, custom headers, query parameters, and request bodies, making it suitable for integrating with external APIs such as SaaS platforms, cloud services, or any HTTP-based API provider.
"""

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

import httpx
from typing import Any, ClassVar, Dict, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field
from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register
from apflow.core.execution.errors import ValidationError, NetworkError
from apflow.logger import get_logger

logger = get_logger(__name__)


class RestAuthConfig(BaseModel):
    type: Literal["bearer", "basic", "apikey"] = Field(description="Authentication type")
    token: Optional[str] = Field(default=None, description="Bearer token (for bearer auth)")
    username: Optional[str] = Field(default=None, description="Username (for basic auth)")
    password: Optional[str] = Field(default=None, description="Password (for basic auth)")
    key: Optional[str] = Field(default=None, description="API key name (for apikey auth)")
    value: Optional[str] = Field(default=None, description="API key value (for apikey auth)")
    location: Literal["header", "query"] = Field(
        default="header", description="Where to place the API key (default: header)"
    )


class RestInputSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(description="Target URL for the HTTP request")
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = Field(
        default="GET", description="HTTP method (default: GET)"
    )
    headers: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional HTTP headers as key-value pairs (merged with defaults)",
    )
    params: Optional[Dict[str, Any]] = Field(
        default=None, description="Query parameters as key-value pairs"
    )
    json_body: Optional[Dict[str, Any]] = Field(
        default=None, alias="json", description="JSON request body"
    )
    data: Optional[Dict[str, Any]] = Field(default=None, description="Form data as key-value pairs")
    auth: Optional[RestAuthConfig] = Field(default=None, description="Authentication configuration")
    timeout: float = Field(default=30.0, description="Request timeout in seconds (default: 30.0)")
    verify: bool = Field(
        default=True, description="Whether to verify SSL certificates (default: true)"
    )


class RestOutputSchema(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool = Field(description="Whether the request was successful")
    status_code: Optional[int] = Field(default=None, description="HTTP status code")
    url: Optional[str] = Field(default=None, description="Final URL after redirects")
    headers: Optional[Dict[str, Any]] = Field(default=None, description="Response headers")
    method: str = Field(description="HTTP method used")
    json_body: Optional[Dict[str, Any]] = Field(
        default=None, alias="json", description="JSON response body (if applicable)"
    )
    text: Optional[str] = Field(default=None, description="Text response body (if applicable)")
    error: Optional[str] = Field(default=None, description="Error message (if applicable)")


@executor_register()
class RestExecutor(BaseTask):
    """
    Executor for calling REST API endpoints, typically provided by third-party services.
    Supports GET, POST, PUT, DELETE, PATCH methods with authentication, custom headers, query parameters, and request bodies.
    This is ideal for integrating with external APIs, SaaS platforms, cloud services, or any HTTP-based API provider.

    Example usage in task schemas:
    {
        "schemas": {
            "method": "rest_executor"  # Executor id
        },
        "inputs": {
            "url": "https://api.example.com/users",
            "method": "GET",
            "headers": {"Authorization": "Bearer token"},
            "timeout": 30
        }
    }
    """

    id = "rest_executor"
    name = "REST API Executor"
    description = "Execute HTTP/REST API requests with authentication and custom headers"
    tags = ["http", "rest", "api", "webhook"]
    examples = [
        "Call external REST API",
        "Send webhook notification",
        "Fetch data from HTTP service",
    ]
    inputs_schema: ClassVar[type[BaseModel]] = RestInputSchema
    outputs_schema: ClassVar[type[BaseModel]] = RestOutputSchema

    # Cancellation support: Can be cancelled by closing the HTTP client
    cancelable: bool = True

    def __init__(self, headers=None, auth=None, verify=True, follow_redirects=True, **kwargs):
        super().__init__(**kwargs)
        self.default_headers = headers or {}
        self.default_auth = auth
        self.default_verify = verify
        self.default_follow_redirects = follow_redirects

    @property
    def type(self) -> str:
        """Extension type identifier for categorization"""
        return "http"

    async def _validate_url_not_private(self, url: str) -> None:
        """Validate that a URL does not target private or internal network addresses.

        Resolves the hostname to IP addresses and checks against private, loopback,
        link-local, and reserved ranges to prevent SSRF attacks.

        Can be bypassed by setting env var APFLOW_REST_ALLOW_PRIVATE_URLS=1.

        Raises:
            ValidationError: If the URL targets a private/reserved address.
        """
        if os.environ.get("APFLOW_REST_ALLOW_PRIVATE_URLS") == "1":
            return

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise ValidationError(f"[{self.id}] URL has no hostname: {url}")

        # socket.getaddrinfo() is a blocking call; running it directly here would
        # stall the entire shared event loop (all other concurrently-running
        # tasks) for as long as DNS resolution takes. Offload it to a thread.
        loop = asyncio.get_running_loop()
        try:
            addr_infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
        except socket.gaierror:
            raise ValidationError(f"[{self.id}] Cannot resolve hostname: {hostname}")

        for addr_info in addr_infos:
            ip_str = addr_info[4][0]
            ip = ipaddress.ip_address(ip_str)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValidationError(
                    f"[{self.id}] URL targets a private/reserved address: {hostname} -> {ip_str}"
                )

    async def _follow_redirects_with_validation(
        self,
        client: httpx.AsyncClient,
        response: httpx.Response,
        max_redirects: int = 20,
    ) -> httpx.Response:
        """Manually follow redirects, re-validating each hop against SSRF checks.

        httpx's built-in follow_redirects=True follows Location headers without
        re-validating the target, so a malicious or compromised server can
        redirect to a private/internal address (e.g. the cloud metadata
        endpoint 169.254.169.254) and bypass _validate_url_not_private entirely,
        since it only ever checked the original URL. Each hop is validated here
        before being followed.
        """
        redirects = 0
        while response.has_redirect_location:
            if redirects >= max_redirects:
                raise NetworkError(
                    f"[{self.id}] Too many redirects",
                    what="Exceeded maximum redirect count",
                    why=f"More than {max_redirects} redirects while requesting {response.url}",
                    how_to_fix="Check the target service for a redirect loop",
                    context={"url": str(response.url), "max_redirects": max_redirects},
                )
            next_request = response.next_request
            if next_request is None:
                break
            await self._validate_url_not_private(str(next_request.url))
            response = await client.send(next_request, follow_redirects=False)
            redirects += 1
        return response

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an HTTP request

        Args:
            inputs: Dictionary containing:
                - url: Target URL (required)
                - method: HTTP method (GET, POST, PUT, DELETE, PATCH, default: GET)
                - headers: Optional HTTP headers dict (merged with default)
                - params: Optional query parameters dict
                - json: Optional JSON body (dict)
                - data: Optional form data (dict)
                - timeout: Optional timeout in seconds (default: 30.0)

        Returns:
            Dictionary with response data (json or body).
        """
        url = inputs.get("url")
        if not url:
            raise ValidationError(f"[{self.id}] url is required in inputs")

        await self._validate_url_not_private(url)

        method = inputs.get("method", "GET").upper()
        headers = {**self.default_headers, **inputs.get("headers", {})}
        params = inputs.get("params")
        json_data = inputs.get("json")
        data = inputs.get("data")
        timeout = inputs.get("timeout", 30.0)

        # Handle authentication from inputs or defaults
        auth_config = inputs.get("auth") or self.default_auth
        auth = None
        if auth_config:
            auth_type = auth_config.get("type", "").lower()
            if auth_type == "bearer":
                token = auth_config.get("token")
                if token:
                    headers.setdefault("Authorization", f"Bearer {token}")
            elif auth_type == "basic":
                username = auth_config.get("username")
                password = auth_config.get("password")
                if username and password:
                    auth = httpx.BasicAuth(username, password)
            elif auth_type == "apikey":
                key = auth_config.get("key")
                value = auth_config.get("value")
                location = auth_config.get("location", "header").lower()
                if key and value:
                    if location == "header":
                        headers[key] = value
                    elif location == "query":
                        if params is None:
                            params = {}
                        params[key] = value

        # Prepare request kwargs. follow_redirects is always disabled at the
        # transport level here — redirects are followed manually below (see
        # _follow_redirects_with_validation) so each hop can be re-validated
        # against SSRF checks.
        request_kwargs = {
            "method": method,
            "url": url,
            "headers": headers,
            "follow_redirects": False,
        }

        if params:
            request_kwargs["params"] = params
        if json_data is not None:
            request_kwargs["json"] = json_data
        elif data is not None:
            request_kwargs["data"] = data
        if auth:
            request_kwargs["auth"] = auth

        logger.info(f"Executing HTTP {method} request to {url}")

        try:
            async with httpx.AsyncClient(
                verify=inputs.get("verify", self.default_verify), timeout=timeout
            ) as client:
                # Check for cancellation before making request
                if self.cancellation_checker and self.cancellation_checker():
                    return {"success": False, "error": "Request was cancelled", "method": method}

                response = await client.request(**request_kwargs)
                if self.default_follow_redirects:
                    response = await self._follow_redirects_with_validation(client, response)

                # Check for cancellation after request
                if self.cancellation_checker and self.cancellation_checker():
                    return {"success": False, "error": "Request was cancelled", "method": method}

                if not (200 <= response.status_code < 300):
                    return {
                        "success": False,
                        "status_code": response.status_code,
                        "url": str(response.url),
                        "headers": dict(response.headers),
                        "text": response.text,
                        "method": method,
                    }

                # Try to parse JSON response
                json_response = None
                try:
                    json_response = response.json()
                except Exception:
                    pass

                if json_response is not None:
                    return {
                        "success": True,
                        "status_code": response.status_code,
                        "url": str(response.url),
                        "headers": dict(response.headers),
                        "json": json_response,
                        "method": method,
                    }
                else:
                    return {
                        "success": True,
                        "status_code": response.status_code,
                        "url": str(response.url),
                        "headers": dict(response.headers),
                        "text": response.text,
                        "method": method,
                    }
        except httpx.TimeoutException:
            raise NetworkError(
                f"[{self.id}] Request timeout",
                what="HTTP request timed out",
                why=f"No response from {url} within {timeout} seconds",
                how_to_fix="1. Check if the target service is running\n2. Increase timeout value\n3. Verify network connectivity",
                context={"url": url, "method": method, "timeout": timeout},
            )
        except httpx.ConnectError as e:
            raise NetworkError(
                f"[{self.id}] Connection failed",
                what="Failed to connect to server",
                why=f"Cannot establish connection to {url}: {str(e)}",
                how_to_fix="1. Verify the URL is correct\n2. Check if the service is running\n3. Check firewall and network settings",
                context={"url": url, "method": method, "error": str(e)},
            )
        except httpx.HTTPError as e:
            raise NetworkError(
                f"[{self.id}] HTTP error",
                what="HTTP request failed",
                why=f"Error during request to {url}: {type(e).__name__}",
                how_to_fix="1. Check the URL and request parameters\n2. Verify authentication if required\n3. Check server logs for details",
                context={
                    "url": url,
                    "method": method,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Provide demo HTTP response data"""
        method = inputs.get("method", "GET").upper()

        # Generate appropriate demo response based on method
        if method == "GET":
            demo_json = {
                "status": "success",
                "data": {"id": "demo-123", "name": "Demo Resource", "value": 42},
            }
        elif method == "POST":
            demo_json = {
                "status": "created",
                "id": "new-resource-456",
                "message": "Resource created successfully",
            }
        else:
            demo_json = {"status": "success", "message": f"{method} operation completed"}

        return {
            "success": True,
            "status_code": 200,
            "url": inputs.get("url", "https://api.example.com/demo"),
            "headers": {"Content-Type": "application/json"},
            "json": demo_json,
            "method": method,
        }
