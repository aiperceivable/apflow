"""
Send Email Executor for sending emails via Resend API or SMTP.

This executor supports two email providers:
- Resend: Cloud email API via HTTP
- SMTP: Traditional SMTP protocol via aiosmtplib (optional dependency)

Environment variables can be used as defaults (input values take priority):
- RESEND_API_KEY: Resend API key
- SMTP_HOST: SMTP server hostname
- SMTP_PORT: SMTP server port (default: 587)
- SMTP_USERNAME: SMTP authentication username
- SMTP_PASSWORD: SMTP authentication password
- SMTP_USE_TLS: Whether to use STARTTLS ("true"/"false", default: "true")
- FROM_EMAIL: Default sender email address

If provider is not specified, it will be auto-detected based on available env vars.
"""

import os
import httpx
from email.message import EmailMessage
from typing import Any, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from apflow.core.base import BaseTask
from apflow.core.execution.errors import ConfigurationError, ValidationError
from apflow.core.extensions.decorators import executor_register
from apflow.logger import get_logger

logger = get_logger(__name__)

try:
    import aiosmtplib

    AIOSMTPLIB_AVAILABLE = True
except ImportError:
    aiosmtplib = None  # type: ignore[assignment]
    AIOSMTPLIB_AVAILABLE = False
    logger.warning(
        "aiosmtplib is not installed. SMTP email provider will not be available. "
        "Install it with: pip install apflow[email]"
    )


class SendEmailInputSchema(BaseModel):
    provider: Optional[Literal["resend", "smtp"]] = Field(
        default=None,
        description="Email provider to use. Auto-detected from env vars if not specified.",
    )
    to: Union[str, List[str]] = Field(description="Recipient email address(es)")
    subject: str = Field(description="Email subject line")
    from_email: Optional[str] = Field(
        default=None,
        description="Sender email address. Falls back to FROM_EMAIL env var.",
    )
    body: Optional[str] = Field(default=None, description="Plain text email body")
    html: Optional[str] = Field(default=None, description="HTML email body")
    cc: Optional[Union[str, List[str]]] = Field(
        default=None, description="CC recipient email address(es)"
    )
    bcc: Optional[Union[str, List[str]]] = Field(
        default=None, description="BCC recipient email address(es)"
    )
    reply_to: Optional[str] = Field(default=None, description="Reply-to email address")
    timeout: float = Field(default=30, description="Request timeout in seconds (default: 30)")
    api_key: Optional[str] = Field(
        default=None,
        description="Resend API key. Falls back to RESEND_API_KEY env var (recommended: "
        "passing it here persists it in cleartext in the task's stored inputs).",
        json_schema_extra={"x-sensitive": True},
    )
    smtp_host: Optional[str] = Field(
        default=None,
        description="SMTP server hostname. Falls back to SMTP_HOST env var.",
    )
    smtp_port: Optional[int] = Field(
        default=None,
        description="SMTP server port. Falls back to SMTP_PORT env var (default: 587).",
    )
    smtp_username: Optional[str] = Field(
        default=None,
        description="SMTP authentication username. Falls back to SMTP_USERNAME env var "
        "(recommended: passing it here persists it in cleartext in the task's stored inputs).",
        json_schema_extra={"x-sensitive": True},
    )
    smtp_password: Optional[str] = Field(
        default=None,
        description="SMTP authentication password. Falls back to SMTP_PASSWORD env var "
        "(recommended: passing it here persists it in cleartext in the task's stored inputs).",
        json_schema_extra={"x-sensitive": True},
    )
    smtp_use_tls: Optional[bool] = Field(
        default=None,
        description="Whether to use STARTTLS. Falls back to SMTP_USE_TLS env var (default: true).",
    )


class SendEmailOutputSchema(BaseModel):
    success: bool = Field(description="Whether the email was sent successfully")
    provider: str = Field(description="Email provider used (resend or smtp)")
    message_id: Optional[str] = Field(
        default=None, description="Message ID from provider (resend only)"
    )
    status_code: Optional[int] = Field(default=None, description="HTTP status code (resend only)")
    message: Optional[str] = Field(default=None, description="Success/error message")
    error: Optional[str] = Field(default=None, description="Error details (on failure)")


@executor_register()
class SendEmailExecutor(BaseTask):
    """
    Executor for sending emails via Resend API or SMTP.

    Supports two providers:
    - "resend": Uses Resend HTTP API (requires api_key)
    - "smtp": Uses SMTP protocol via aiosmtplib (requires smtp_host, etc.)

    Credentials (api_key, smtp_username, smtp_password) are marked x-sensitive
    so they are redacted from observability logging, but they are still stored
    in cleartext in the task's persisted inputs if passed here. Prefer
    configuring RESEND_API_KEY / SMTP_USERNAME / SMTP_PASSWORD as environment
    variables instead — inputs are only a fallback for cases that need
    per-task overrides.

    Example usage in task schemas (credentials from env vars, not inputs):
    {
        "schemas": {
            "method": "send_email_executor"
        },
        "inputs": {
            "provider": "resend",
            "to": ["user@example.com"],
            "from_email": "noreply@example.com",
            "subject": "Hello",
            "body": "Hello World"
        }
    }
    """

    id = "send_email_executor"
    name = "Send Email Executor"
    description = "Send emails via Resend API or SMTP"
    tags = ["email", "notification", "smtp", "resend"]
    examples = [
        "Send transactional email via Resend",
        "Send notification email via SMTP",
        "Send HTML email with cc/bcc recipients",
    ]

    cancelable: bool = False
    inputs_schema: ClassVar[type[BaseModel]] = SendEmailInputSchema
    outputs_schema: ClassVar[type[BaseModel]] = SendEmailOutputSchema

    @property
    def type(self) -> str:
        """Extension type identifier for categorization"""
        return "email"

    def _normalize_recipients(self, value: Union[str, List[str], None]) -> List[str]:
        """Normalize recipient field to a list of strings."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def _merge_env_defaults(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge environment variable defaults with inputs.

        Input values take priority over env vars.
        """
        merged = dict(inputs)

        # Resend config
        if not merged.get("api_key"):
            merged["api_key"] = os.getenv("RESEND_API_KEY")

        # SMTP config
        if not merged.get("smtp_host"):
            merged["smtp_host"] = os.getenv("SMTP_HOST")
        if merged.get("smtp_port") is None:
            env_port = os.getenv("SMTP_PORT")
            merged["smtp_port"] = int(env_port) if env_port else 587
        if not merged.get("smtp_username"):
            merged["smtp_username"] = os.getenv("SMTP_USERNAME")
        if not merged.get("smtp_password"):
            merged["smtp_password"] = os.getenv("SMTP_PASSWORD")
        if merged.get("smtp_use_tls") is None:
            env_tls = os.getenv("SMTP_USE_TLS", "true").lower()
            merged["smtp_use_tls"] = env_tls not in ("false", "0", "no")

        # Shared config
        if not merged.get("from_email"):
            merged["from_email"] = os.getenv("FROM_EMAIL")

        # Auto-detect provider if not specified
        if not merged.get("provider"):
            if merged.get("api_key"):
                merged["provider"] = "resend"
            elif merged.get("smtp_host"):
                merged["provider"] = "smtp"

        return merged

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send an email using the specified provider.

        Args:
            inputs: Dictionary containing:
                - provider: "resend" or "smtp" (auto-detected from env if not set)
                - to: Recipient email(s), str or list (required)
                - subject: Email subject (required)
                - from_email: Sender email address (falls back to FROM_EMAIL env)
                - body: Plain text body (required if html not provided)
                - html: HTML body (optional, alternative to body)
                - cc: CC recipients, str or list (optional)
                - bcc: BCC recipients, str or list (optional)
                - reply_to: Reply-to address (optional)
                - timeout: Request timeout in seconds (default: 30)
                Resend-specific:
                - api_key: Resend API key (falls back to RESEND_API_KEY env)
                SMTP-specific:
                - smtp_host: SMTP server hostname (falls back to SMTP_HOST env)
                - smtp_port: SMTP server port (falls back to SMTP_PORT env, default: 587)
                - smtp_username: SMTP auth username (falls back to SMTP_USERNAME env)
                - smtp_password: SMTP auth password (falls back to SMTP_PASSWORD env)
                - smtp_use_tls: Whether to use STARTTLS (falls back to SMTP_USE_TLS env)

        Returns:
            Dictionary with send results.
        """
        # Merge env var defaults with inputs (inputs take priority)
        merged = self._merge_env_defaults(inputs)

        provider = merged.get("provider")
        if not provider:
            raise ValidationError(
                f"[{self.id}] provider is required. "
                "Set provider in inputs or configure RESEND_API_KEY or SMTP_HOST env var."
            )

        to = merged.get("to")
        if not to:
            raise ValidationError(f"[{self.id}] to is required")

        subject = merged.get("subject")
        if not subject:
            raise ValidationError(f"[{self.id}] subject is required")

        from_email = merged.get("from_email")
        if not from_email:
            raise ValidationError(
                f"[{self.id}] from_email is required. "
                "Set from_email in inputs or configure FROM_EMAIL env var."
            )

        body = merged.get("body")
        html = merged.get("html")
        if not body and not html:
            raise ValidationError(f"[{self.id}] Either body or html must be provided")

        if provider == "resend":
            return await self._send_via_resend(merged)
        elif provider == "smtp":
            return await self._send_via_smtp(merged)
        else:
            raise ValidationError(
                f"[{self.id}] Unknown provider '{provider}'. Supported: 'resend', 'smtp'"
            )

    async def _send_via_resend(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Send email via Resend HTTP API."""
        api_key = inputs.get("api_key")
        if not api_key:
            raise ValidationError(
                f"[{self.id}] api_key is required for resend provider. "
                "Set api_key in inputs or configure RESEND_API_KEY env var."
            )

        to_list = self._normalize_recipients(inputs.get("to"))
        cc_list = self._normalize_recipients(inputs.get("cc"))
        bcc_list = self._normalize_recipients(inputs.get("bcc"))
        timeout = inputs.get("timeout", 30)

        payload: Dict[str, Any] = {
            "from": inputs["from_email"],
            "to": to_list,
            "subject": inputs["subject"],
        }

        if inputs.get("html"):
            payload["html"] = inputs["html"]
        if inputs.get("body"):
            payload["text"] = inputs["body"]
        if cc_list:
            payload["cc"] = cc_list
        if bcc_list:
            payload["bcc"] = bcc_list
        if inputs.get("reply_to"):
            payload["reply_to"] = inputs["reply_to"]

        logger.info(f"Sending email via Resend to {to_list}")

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        if 200 <= response.status_code < 300:
            response_data = response.json()
            return {
                "success": True,
                "provider": "resend",
                "message_id": response_data.get("id"),
                "status_code": response.status_code,
            }
        else:
            return {
                "success": False,
                "provider": "resend",
                "status_code": response.status_code,
                "error": response.text,
            }

    async def _send_via_smtp(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Send email via SMTP using aiosmtplib."""
        if not AIOSMTPLIB_AVAILABLE:
            raise ConfigurationError(
                f"[{self.id}] aiosmtplib is not installed. "
                "Install it with: pip install apflow[email]"
            )

        smtp_host = inputs.get("smtp_host")
        if not smtp_host:
            raise ValidationError(
                f"[{self.id}] smtp_host is required for smtp provider. "
                "Set smtp_host in inputs or configure SMTP_HOST env var."
            )

        smtp_port = inputs.get("smtp_port", 587)
        smtp_username = inputs.get("smtp_username")
        smtp_password = inputs.get("smtp_password")
        smtp_use_tls = inputs.get("smtp_use_tls", True)
        timeout = inputs.get("timeout", 30)

        to_list = self._normalize_recipients(inputs.get("to"))
        cc_list = self._normalize_recipients(inputs.get("cc"))
        bcc_list = self._normalize_recipients(inputs.get("bcc"))

        msg = EmailMessage()
        msg["From"] = inputs["from_email"]
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = inputs["subject"]

        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        if inputs.get("reply_to"):
            msg["Reply-To"] = inputs["reply_to"]

        body = inputs.get("body")
        html = inputs.get("html")

        if body:
            msg.set_content(body)
        if html:
            if body:
                msg.add_alternative(html, subtype="html")
            else:
                msg.set_content(html, subtype="html")

        all_recipients = to_list + cc_list + bcc_list

        logger.info(f"Sending email via SMTP ({smtp_host}:{smtp_port}) to {to_list}")

        send_kwargs: Dict[str, Any] = {
            "message": msg,
            "hostname": smtp_host,
            "port": smtp_port,
            "recipients": all_recipients,
            "timeout": timeout,
        }

        if smtp_use_tls:
            send_kwargs["start_tls"] = True
        if smtp_username:
            send_kwargs["username"] = smtp_username
        if smtp_password:
            send_kwargs["password"] = smtp_password

        await aiosmtplib.send(**send_kwargs)  # type: ignore[possibly-undefined]

        return {
            "success": True,
            "provider": "smtp",
            "message": f"Email sent to {', '.join(to_list)}",
        }

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Provide demo email send result."""
        provider = inputs.get("provider", "resend")
        to = inputs.get("to", ["demo@example.com"])
        if isinstance(to, str):
            to = [to]

        if provider == "smtp":
            return {
                "success": True,
                "provider": "smtp",
                "message": f"Email sent to {', '.join(to)}",
                "_demo_sleep": 0.3,
            }

        return {
            "success": True,
            "provider": "resend",
            "message_id": "demo-msg-id-123",
            "status_code": 200,
            "_demo_sleep": 0.3,
        }
