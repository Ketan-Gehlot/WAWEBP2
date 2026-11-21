"""
Official Meta WhatsApp Cloud API HTTPX Client.

Integrates exclusively with https://graph.facebook.com/{version}/{phone_number_id}/messages.
Never uses unofficial endpoints, browser scraping, or reverse-engineered clients.
"""
from dataclasses import dataclass
from typing import Any, Optional
import uuid
import httpx

from app.config import settings
from app.core.logging import log_structured_event

# Meta API error codes that are permanent and MUST NOT be retried blindly
PERMANENT_META_ERROR_CODES = {
    100,     # Invalid parameter
    190,     # Invalid OAuth access token
    200,     # Permission error
    368,     # Temporarily blocked for policy violations (account quality halt)
    131000,  # Something went wrong (non-retryable payload error)
    131005,  # Access denied
    131008,  # Required parameter is missing
    131009,  # Parameter value is not valid
    131021,  # Recipient cannot be sender
    131026,  # Message undeliverable / recipient not on WhatsApp
    131031,  # Account has been locked / restricted
    131051,  # Unsupported message type
    132000,  # Template parameter count mismatch
    132001,  # Template does not exist in specified language
    132005,  # Template hydrated text too long
    132007,  # Template character policy violated
    132012,  # Template parameter format mismatch
    132015,  # Template is paused due to low quality
    132016,  # Template is disabled
    133010,  # Phone number not registered
}

ACCOUNT_RESTRICTION_ERROR_CODES = {368, 131031}
RETRYABLE_META_ERROR_CODES = {2, 4, 130429, 131048, 131053}
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class WhatsAppAPIResult:
    """Structured result returned from the Meta WhatsApp Cloud API client."""

    success: bool
    whatsapp_message_id: Optional[str] = None
    status_label: str = "sent"  # sent, dry_run, failed
    http_status: Optional[int] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    is_retryable: bool = False
    triggers_account_pause: bool = False
    raw_response: Optional[dict[str, Any]] = None


class WhatsAppCloudClient:
    """Official Meta WhatsApp Cloud API client using HTTPX."""

    def __init__(self, http_client: Optional[httpx.Client] = None) -> None:
        self._client = http_client

    @property
    def messages_url(self) -> str:
        base = settings.META_GRAPH_BASE_URL.rstrip("/")
        version = settings.META_GRAPH_API_VERSION.strip("/")
        phone_id = settings.META_PHONE_NUMBER_ID.strip("/")
        return f"{base}/{version}/{phone_id}/messages"

    @property
    def templates_url(self) -> str:
        base = settings.META_GRAPH_BASE_URL.rstrip("/")
        version = settings.META_GRAPH_API_VERSION.strip("/")
        waba_id = settings.META_WABA_ID.strip("/")
        return f"{base}/{version}/{waba_id}/message_templates"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def build_text_payload(recipient_phone: str, text: str, preview_url: bool = False) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": text,
            },
        }

    @staticmethod
    def build_template_payload(
        recipient_phone: str,
        template_name: str,
        language_code: str = "en_US",
        body_params: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        components: list[dict[str, Any]] = []
        if body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(param)} for param in body_params
                    ],
                }
            )
        template_obj: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if components:
            template_obj["components"] = components

        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": "template",
            "template": template_obj,
        }

    @staticmethod
    def build_media_payload(
        recipient_phone: str,
        media_type: str,
        media_url: str,
        caption: Optional[str] = None,
    ) -> dict[str, Any]:
        media_obj: dict[str, Any] = {"link": media_url}
        if caption and media_type in {"image", "document", "video"}:
            media_obj["caption"] = caption
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": media_type,
            media_type: media_obj,
        }

    @staticmethod
    def build_interactive_payload(
        recipient_phone: str,
        interactive_spec: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": "interactive",
            "interactive": interactive_spec,
        }

    def send_payload(
        self,
        payload: dict[str, Any],
        force_dry_run: bool = False,
    ) -> WhatsAppAPIResult:
        """
        Send a formatted payload to Meta's /messages endpoint.
        Honors DRY_RUN and ENABLE_OUTBOUND_MESSAGES safety flags.
        """
        if force_dry_run or settings.DRY_RUN or not settings.ENABLE_OUTBOUND_MESSAGES:
            simulated_id = f"wamid.DRYRUN.{uuid.uuid4().hex[:20]}"
            log_structured_event(
                "outbound_dry_run",
                "Outbound WhatsApp message processed in DRY_RUN / outbound-disabled safety mode",
                whatsapp_message_id=simulated_id,
                dry_run=settings.DRY_RUN,
                enable_outbound=settings.ENABLE_OUTBOUND_MESSAGES,
                payload_type=payload.get("type"),
            )
            return WhatsAppAPIResult(
                success=True,
                whatsapp_message_id=simulated_id,
                status_label="dry_run",
                http_status=200,
                raw_response={"simulated": True, "messages": [{"id": simulated_id}]},
            )

        if not settings.META_PHONE_NUMBER_ID or not settings.META_ACCESS_TOKEN:
            log_structured_event(
                "meta_api_error",
                "Meta WhatsApp Cloud API credentials are not configured",
                reason="MISSING_META_CREDENTIALS",
            )
            return WhatsAppAPIResult(
                success=False,
                status_label="failed",
                error_code="MISSING_META_CREDENTIALS",
                error_message=(
                    "META_PHONE_NUMBER_ID and META_ACCESS_TOKEN must be configured when "
                    "ENABLE_OUTBOUND_MESSAGES=true and DRY_RUN=false."
                ),
                is_retryable=False,
            )

        close_client = False
        client = self._client
        if client is None:
            client = httpx.Client(timeout=settings.META_HTTP_TIMEOUT_SECONDS)
            close_client = True

        try:
            response = client.post(
                self.messages_url,
                headers=self._headers(),
                json=payload,
            )
            http_status = response.status_code
            try:
                data = response.json()
            except Exception:
                data = {"raw_text": response.text}

            if 200 <= http_status < 300:
                messages_list = data.get("messages") or []
                wamid = messages_list[0].get("id") if messages_list else None
                log_structured_event(
                    "outbound_send_success",
                    "Successfully delivered outbound request to Meta WhatsApp Cloud API",
                    whatsapp_message_id=wamid,
                    http_status=http_status,
                )
                return WhatsAppAPIResult(
                    success=True,
                    whatsapp_message_id=wamid,
                    status_label="sent",
                    http_status=http_status,
                    raw_response=data,
                )

            # Parse structured Meta Graph API error object
            error_obj = data.get("error") if isinstance(data, dict) else {}
            if not isinstance(error_obj, dict):
                error_obj = {}
            meta_code = error_obj.get("code")
            meta_subcode = error_obj.get("error_subcode")
            meta_message = error_obj.get("message") or f"Meta Graph API returned HTTP {http_status}"

            is_permanent = meta_code in PERMANENT_META_ERROR_CODES or (
                400 <= http_status < 500 and http_status != 429
            )
            is_retryable = not is_permanent and (
                meta_code in RETRYABLE_META_ERROR_CODES or http_status in RETRYABLE_HTTP_STATUS_CODES
            )
            triggers_pause = meta_code in ACCOUNT_RESTRICTION_ERROR_CODES

            error_code_str = f"META_{meta_code}" if meta_code is not None else f"HTTP_{http_status}"
            if meta_subcode:
                error_code_str = f"{error_code_str}_{meta_subcode}"

            log_structured_event(
                "meta_api_error",
                f"Meta WhatsApp Cloud API error: {error_code_str} - {meta_message}",
                http_status=http_status,
                error_code=error_code_str,
                is_retryable=is_retryable,
                triggers_account_pause=triggers_pause,
            )
            return WhatsAppAPIResult(
                success=False,
                status_label="failed",
                http_status=http_status,
                error_code=error_code_str,
                error_message=meta_message,
                is_retryable=is_retryable,
                triggers_account_pause=triggers_pause,
                raw_response=data,
            )

        except httpx.TimeoutException as exc:
            log_structured_event(
                "meta_api_error",
                "Timeout communicating with Meta WhatsApp Cloud API",
                error_code="NETWORK_TIMEOUT",
                is_retryable=True,
            )
            return WhatsAppAPIResult(
                success=False,
                status_label="failed",
                error_code="NETWORK_TIMEOUT",
                error_message=f"Request to Meta WhatsApp Cloud API timed out: {exc}",
                is_retryable=True,
            )
        except httpx.HTTPError as exc:
            log_structured_event(
                "meta_api_error",
                "HTTP transport error communicating with Meta WhatsApp Cloud API",
                error_code="NETWORK_ERROR",
                is_retryable=True,
            )
            return WhatsAppAPIResult(
                success=False,
                status_label="failed",
                error_code="NETWORK_ERROR",
                error_message=f"HTTP transport error: {exc}",
                is_retryable=True,
            )
        finally:
            if close_client and client is not None:
                client.close()


whatsapp_client = WhatsAppCloudClient()
