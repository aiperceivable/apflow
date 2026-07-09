"""
Test ApFlowApiExecutor

Tests for apflow API executor functionality.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
from apflow.extensions.apflow.api_executor import ApFlowApiExecutor


class TestApFlowApiExecutor:
    """Test ApFlowApiExecutor functionality"""

    @pytest.fixture(autouse=True)
    def _mock_dns_resolution(self):
        """Mock DNS resolution to return a public IP for test URLs.

        This prevents the SSRF validation from failing on the loopback
        "localhost" hostname used throughout the existing tests below.
        """
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            yield

    @pytest.mark.asyncio
    async def test_execute_api_call(self):
        """Test executing an API call"""
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123", "status": "started"},
            "id": "test-id",
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {"task_id": "task-123"},
                }
            )

            assert result["success"] is True
            assert result["result"]["task_id"] == "task-123"
            mock_client_instance.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_api_call_with_auth(self):
        """Test executing API call with authentication"""
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123"},
            "id": "test-id",
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.get",
                    "params": {"task_id": "task-123"},
                    "auth_token": "test-token",
                }
            )

            assert result["success"] is True
            call_kwargs = mock_client_instance.post.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-token"

    @pytest.mark.asyncio
    async def test_execute_api_error_response(self):
        """Test handling API error response"""
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
            "id": "test-id",
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {"base_url": "http://localhost:8000", "method": "tasks.invalid", "params": {}}
            )

            assert result["success"] is False
            assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_missing_base_url(self):
        """Test error when base_url is missing"""
        executor = ApFlowApiExecutor()

        with pytest.raises(ValueError, match="base_url is required"):
            await executor.execute({"method": "tasks.execute", "params": {}})

    @pytest.mark.asyncio
    async def test_execute_missing_method(self):
        """Test error when method is missing"""
        executor = ApFlowApiExecutor()

        with pytest.raises(ValueError, match="method is required"):
            await executor.execute({"base_url": "http://localhost:8000", "params": {}})

    @pytest.mark.asyncio
    async def test_execute_timeout_error(self):
        """Test handling timeout errors"""
        executor = ApFlowApiExecutor()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {"task_id": "task-123"},
                    "timeout": 5.0,
                }
            )

            assert result["success"] is False
            assert "timeout" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_wait_for_completion(self):
        """Test waiting for task completion"""
        executor = ApFlowApiExecutor()

        # Mock initial execute response
        mock_execute_response = MagicMock()
        mock_execute_response.status_code = 200
        mock_execute_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123", "status": "started"},
            "id": "test-id",
        }

        # Mock polling responses
        mock_poll_responses = [
            MagicMock(
                status_code=200,
                json=lambda: {
                    "jsonrpc": "2.0",
                    "result": {"id": "task-123", "status": "in_progress"},
                    "id": "poll-1",
                },
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "jsonrpc": "2.0",
                    "result": {
                        "id": "task-123",
                        "status": "completed",
                        "result": {"output": "done"},
                    },
                    "id": "poll-2",
                },
            ),
        ]

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(
                side_effect=[mock_execute_response] + mock_poll_responses
            )

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {"task_id": "task-123"},
                    "wait_for_completion": True,
                    "poll_interval": 0.1,
                }
            )

            assert result["success"] is True
            assert result["status"] == "completed"
            # Should have called post multiple times (execute + polls)
            assert mock_client_instance.post.call_count >= 2

    @pytest.mark.asyncio
    async def test_execute_wait_for_completion_timeout(self):
        """Test timeout while waiting for task completion"""
        executor = ApFlowApiExecutor()

        async def skip_url_validation(*args, **kwargs):
            return None

        mock_execute_response = MagicMock()
        mock_execute_response.status_code = 200
        mock_execute_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123", "status": "started"},
            "id": "test-id",
        }

        # Mock polling that never completes - return in_progress status
        def create_poll_response():
            mock_poll = MagicMock()
            mock_poll.status_code = 200
            mock_poll.json.return_value = {
                "jsonrpc": "2.0",
                "result": {"id": "task-123", "status": "in_progress"},
                "id": "poll-1",
            }
            return mock_poll

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance

            # First call is execute, then all subsequent calls are polls
            call_count = [0]

            def post_side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock_execute_response
                else:
                    return create_poll_response()

            mock_client_instance.post = AsyncMock(side_effect=post_side_effect)

            # Mock time to simulate timeout - use a simple counter
            import asyncio

            time_counter = [0.0]

            def mock_time():
                # First call returns start time, subsequent calls increment
                if time_counter[0] == 0.0:
                    time_counter[0] = 100.0  # Start time
                    return 100.0
                else:
                    time_counter[0] += 2.0  # Jump past timeout (1.0)
                    return time_counter[0]

            loop = asyncio.get_event_loop()
            with (
                patch.object(loop, "time", side_effect=mock_time),
                patch(
                    "apflow.extensions.apflow.api_executor.validate_url_not_private",
                    side_effect=skip_url_validation,
                ),
            ):
                result = await executor.execute(
                    {
                        "base_url": "http://localhost:8000",
                        "method": "tasks.execute",
                        "params": {"task_id": "task-123"},
                        "wait_for_completion": True,
                        "poll_interval": 0.01,  # Very short interval
                        "timeout": 1.0,
                    }
                )

            # Should timeout
            assert result["success"] is False
            assert "timeout" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_non_200_status(self):
        """Test handling non-200 HTTP status codes"""
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {"task_id": "task-123"},
                }
            )

            assert result["success"] is False
            assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_execute_request_error(self):
        """Test handling request errors"""
        executor = ApFlowApiExecutor()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(
                side_effect=httpx.RequestError("Connection error")
            )

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {"task_id": "task-123"},
                }
            )

            assert result["success"] is False
            assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_cancellation_before_request(self):
        """Test cancellation before making request"""
        executor = ApFlowApiExecutor()
        executor.cancellation_checker = lambda: True

        result = await executor.execute(
            {
                "base_url": "http://localhost:8000",
                "method": "tasks.execute",
                "params": {"task_id": "task-123"},
            }
        )

        assert result["success"] is False
        assert "cancelled" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_different_api_methods(self):
        """Test different API methods"""
        executor = ApFlowApiExecutor()

        methods = ["tasks.create", "tasks.get", "tasks.update", "tasks.delete", "tasks.list"]

        for method in methods:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "jsonrpc": "2.0",
                "result": {"success": True},
                "id": "test-id",
            }

            with patch("httpx.AsyncClient") as mock_client:
                mock_client_instance = AsyncMock()
                mock_client.return_value.__aenter__.return_value = mock_client_instance
                mock_client_instance.post = AsyncMock(return_value=mock_response)

                result = await executor.execute(
                    {"base_url": "http://localhost:8000", "method": method, "params": {}}
                )

                assert result["success"] is True
                call_args = mock_client_instance.post.call_args
                request_data = call_args[1]["json"]
                assert request_data["method"] == method

    @pytest.mark.asyncio
    async def test_execute_with_custom_headers(self):
        """Test executing API call with custom headers"""
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123"},
            "id": "test-id",
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.get",
                    "params": {"task_id": "task-123"},
                    "headers": {"X-Custom-Header": "custom-value"},
                }
            )

            assert result["success"] is True
            call_kwargs = mock_client_instance.post.call_args[1]
            assert call_kwargs["headers"]["X-Custom-Header"] == "custom-value"

    @pytest.mark.asyncio
    async def test_execute_rejects_private_base_url(self):
        """Regression: base_url is task-supplied and was used for outbound
        HTTP calls with zero SSRF validation, unlike sibling rest_executor.py
        (which has _validate_url_not_private). A task could target internal
        infrastructure (e.g. the cloud metadata endpoint) via this executor.
        (Review CRITICAL #57)
        """
        executor = ApFlowApiExecutor()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"success": True},
            "id": "test-id",
        }

        with (
            patch("httpx.AsyncClient") as mock_client,
            patch(
                "socket.getaddrinfo",
                return_value=[(None, None, None, None, ("169.254.169.254", 0))],
            ),
        ):
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_response)

            result = await executor.execute(
                {
                    "base_url": "http://metadata.internal",
                    "method": "tasks.execute",
                    "params": {},
                }
            )

        assert result["success"] is False
        assert "private" in result["error"].lower() or "reserved" in result["error"].lower()
        # The outbound call must never have been attempted.
        mock_client_instance.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_rejects_private_base_url_during_polling(self):
        """Regression: the same missing SSRF check applied to the polling
        loop's outbound requests in _wait_for_task_completion. (Review
        CRITICAL #57)
        """
        executor = ApFlowApiExecutor()

        mock_execute_response = MagicMock()
        mock_execute_response.status_code = 200
        mock_execute_response.json.return_value = {
            "jsonrpc": "2.0",
            "result": {"task_id": "task-123", "status": "started"},
            "id": "test-id",
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_client_instance = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_client_instance
            mock_client_instance.post = AsyncMock(return_value=mock_execute_response)

            with patch(
                "socket.getaddrinfo",
                return_value=[(None, None, None, None, ("10.0.0.5", 0))],
            ):
                result = await executor.execute(
                    {
                        "base_url": "http://internal-host",
                        "method": "tasks.execute",
                        "params": {"task_id": "task-123"},
                        "wait_for_completion": True,
                        "poll_interval": 0.01,
                    }
                )

        assert result["success"] is False
        assert "private" in result["error"].lower() or "reserved" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execute_use_streaming_raises_instead_of_silently_ignoring(self):
        """Regression: use_streaming was parsed but discarded — the documented
        streaming mode was never implemented, so a caller requesting it got no
        indication their request was ignored. (Review CRITICAL #58)
        """
        executor = ApFlowApiExecutor()

        with pytest.raises(ValueError, match="use_streaming"):
            await executor.execute(
                {
                    "base_url": "http://localhost:8000",
                    "method": "tasks.execute",
                    "params": {},
                    "use_streaming": True,
                }
            )

    @pytest.mark.asyncio
    async def test_get_input_schema(self):
        """Test input schema generation"""
        executor = ApFlowApiExecutor()
        schema = executor.get_input_schema()

        assert schema["type"] == "object"
        assert "base_url" in schema["required"]
        assert "method" in schema["required"]
        assert "params" in schema["required"]
        assert "wait_for_completion" in schema["properties"]
        assert "poll_interval" in schema["properties"]
