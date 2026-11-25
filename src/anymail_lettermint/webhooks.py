"""
Lettermint tracking and inbound webhooks for django-anymail.

Follows Anymail's webhook architecture (anymail/webhooks/base.py, resend.py):
each view validates the request, parses the JSON body into a normalized
Anymail event, and sends Anymail's ``tracking`` or ``inbound`` signal.

Lettermint signs every delivery (https://lettermint.co/docs/platform/webhooks/signing)::

    X-Lettermint-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256>
    X-Lettermint-Delivery:  <unix seconds>   (must equal t)

The HMAC is over ``"<t>." + raw body`` keyed with the webhook's signing secret
(``whsec_...``, used as is). The checks are ported from the official SDK's
``lettermint/webhook.py``.
"""

import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from email.utils import formataddr

import requests
from anymail.exceptions import AnymailConfigurationError, AnymailWebhookValidationFailure
from anymail.inbound import AnymailInboundMessage
from anymail.signals import (
    AnymailInboundEvent,
    AnymailTrackingEvent,
    EventType,
    RejectReason,
    inbound,
    tracking,
)
from anymail.utils import get_anymail_setting
from anymail.webhooks.base import AnymailBaseWebhookView, AnymailCoreWebhookView

__all__ = [
    "LettermintInboundWebhookView",
    "LettermintSignatureMixin",
    "LettermintTrackingWebhookView",
    "verify_signature",
]

#: Maximum difference between the signed timestamp and now, in seconds
#: (the SDK's and the docs' default).
SIGNATURE_TOLERANCE = 300

_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def verify_signature(body, signature_header, delivery_header, secret, now=None):
    """
    Raise AnymailWebhookValidationFailure unless the body is signed with secret.

    Port of ``Webhook.verify_signature`` from the lettermint SDK: the timestamp
    must be within SIGNATURE_TOLERANCE seconds of now, equal the
    X-Lettermint-Delivery header, and any ``v1`` signature may match
    (compared in constant time).
    """
    if not signature_header:
        raise AnymailWebhookValidationFailure("Missing X-Lettermint-Signature header")
    timestamp = None
    signatures = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and timestamp is None and value.isdigit() and value.isascii():
            timestamp = value
        elif key == "v1" and _HEX_SHA256.fullmatch(value):
            signatures.append(bytes.fromhex(value))
    if timestamp is None or not signatures:
        raise AnymailWebhookValidationFailure("Malformed X-Lettermint-Signature header")
    if (delivery_header or "").strip() != timestamp:
        raise AnymailWebhookValidationFailure(
            "X-Lettermint-Delivery header does not match the signed timestamp"
        )
    now = time.time() if now is None else now
    if abs(int(now) - int(timestamp)) > SIGNATURE_TOLERANCE:
        raise AnymailWebhookValidationFailure("Lettermint webhook timestamp is out of tolerance")
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).digest()
    # Check every candidate, so timing doesn't reveal which one matched.
    matched = False
    for candidate in signatures:
        matched |= hmac.compare_digest(candidate, expected)
    if not matched:
        raise AnymailWebhookValidationFailure("Lettermint webhook called with incorrect signature")


class LettermintSignatureMixin(AnymailCoreWebhookView):
    """Validates X-Lettermint-Signature with the secret named by secret_setting_name."""

    # Each Lettermint webhook endpoint has its own signing secret.
    secret_setting_name = "webhook_secret"

    @classmethod
    def as_view(cls, **initkwargs):
        # View.as_view only accepts kwargs that already exist as class attributes.
        if not hasattr(cls, cls.secret_setting_name):
            setattr(cls, cls.secret_setting_name, None)
        return super().as_view(**initkwargs)

    def __init__(self, **kwargs):
        # Required: an unsigned Lettermint webhook would accept forged events.
        self.signing_secret = get_anymail_setting(
            self.secret_setting_name, esp_name=self.esp_name, kwargs=kwargs
        )
        self.warn_if_no_basic_auth = False  # the signature authenticates the request
        super().__init__(**kwargs)

    def validate_request(self, request):
        verify_signature(
            request.body,
            request.headers.get("X-Lettermint-Signature"),
            request.headers.get("X-Lettermint-Delivery"),
            self.signing_secret,
        )


def _parse_timestamp(value):
    try:
        # fromisoformat() accepts "Z" only from Python 3.11
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _load_json(request):
    try:
        esp_event = json.loads(request.body.decode("utf-8"))
    except ValueError as err:
        raise AnymailWebhookValidationFailure("Lettermint webhook body is not JSON") from err
    if not isinstance(esp_event, dict):
        raise AnymailWebhookValidationFailure("Lettermint webhook body is not a JSON object")
    return esp_event


class LettermintTrackingWebhookView(LettermintSignatureMixin, AnymailBaseWebhookView):
    """Handler for Lettermint message.* status and engagement webhooks"""

    # https://lettermint.co/docs/platform/webhooks/events

    esp_name = "Lettermint"
    signal = tracking

    # Lettermint event: (Anymail event_type, reject_reason)
    event_types = {
        "message.created": (EventType.QUEUED, None),
        "message.scheduled": (EventType.QUEUED, None),
        "message.sent": (EventType.SENT, None),
        "message.delivered": (EventType.DELIVERED, None),
        "message.auto_replied": (EventType.AUTORESPONDED, None),
        "message.hard_bounced": (EventType.BOUNCED, RejectReason.BOUNCED),
        # A 4xx (temporary) SMTP response.
        "message.soft_bounced": (EventType.DEFERRED, None),
        "message.spam_complaint": (EventType.COMPLAINED, RejectReason.SPAM),
        "message.failed": (EventType.FAILED, RejectReason.OTHER),
        "message.suppressed": (EventType.REJECTED, RejectReason.BLOCKED),
        "message.policy_rejected": (EventType.REJECTED, RejectReason.SPAM),
        "message.unsubscribed": (EventType.UNSUBSCRIBED, RejectReason.UNSUBSCRIBED),
        "message.opened": (EventType.OPENED, None),
        "message.clicked": (EventType.CLICKED, None),
        # Anything else (message.canceled, message.rescheduled, message.released,
        # suppression.*, webhook.test, ...) is EventType.UNKNOWN; see esp_event.
    }

    def parse_events(self, request):
        return [self.esp_to_anymail_event(_load_json(request))]

    def esp_to_anymail_event(self, esp_event):
        event_type, reject_reason = self.event_types.get(
            esp_event.get("event"), (EventType.UNKNOWN, None)
        )
        data = esp_event.get("data")
        if not isinstance(data, dict):
            data = {}
        response = data.get("response")
        if not isinstance(response, dict):
            response = {}
        tag = data.get("tag")
        metadata = data.get("metadata")
        return AnymailTrackingEvent(
            event_type=event_type,
            timestamp=_parse_timestamp(esp_event.get("timestamp")),
            # The delivery id, reused when Lettermint retries the delivery.
            event_id=esp_event.get("id"),
            message_id=data.get("message_id"),
            # message.created covers every recipient, so it has no "recipient".
            recipient=data.get("recipient"),
            reject_reason=reject_reason,
            description=data.get("reason"),
            mta_response=response.get("content"),
            # Lettermint's name/value "tags" have no Anymail equivalent; see esp_event.
            tags=[tag] if tag else [],
            metadata=metadata if isinstance(metadata, dict) else {},
            click_url=data.get("destination_url"),
            user_agent=data.get("user_agent"),
            esp_event=esp_event,
        )


class LettermintInboundWebhookView(LettermintSignatureMixin, AnymailBaseWebhookView):
    """Handler for Lettermint message.inbound webhooks"""

    # https://lettermint.co/docs/platform/inbound-mail/process-inbound-email

    esp_name = "Lettermint"
    signal = inbound
    secret_setting_name = "inbound_secret"

    def parse_events(self, request):
        esp_event = _load_json(request)
        if esp_event.get("event") != "message.inbound":
            raise AnymailConfigurationError(
                f"You seem to have set Lettermint's *{esp_event.get('event')}* webhook"
                f" to Anymail's Lettermint *inbound* webhook URL."
            )
        return [self.esp_to_anymail_event(esp_event)]

    def esp_to_anymail_event(self, esp_event):
        data = esp_event.get("data") or {}
        message = AnymailInboundMessage.construct(
            from_email=self._format_address(data.get("from")),
            to=self._format_addresses(data.get("to")),
            cc=self._format_addresses(data.get("cc")),
            subject=data.get("subject"),
            headers=[(h["name"], h["value"]) for h in data.get("headers") or []],
            text=(data.get("body") or {}).get("text"),
            html=(data.get("body") or {}).get("html"),
            attachments=[self._make_attachment(att) for att in data.get("attachments") or []],
        )
        envelope = data.get("envelope") or {}
        message.envelope_sender = envelope.get("mail_from")
        # Lettermint: route on data.recipient, not the To header.
        message.envelope_recipient = data.get("recipient")
        message.spam_detected = data.get("is_spam")
        message.spam_score = data.get("spam_score")
        return AnymailInboundEvent(
            event_type=EventType.INBOUND,
            timestamp=_parse_timestamp(esp_event.get("timestamp")),
            event_id=esp_event.get("id"),
            esp_event=esp_event,
            message=message,
        )

    @staticmethod
    def _format_address(address):
        # Lettermint addresses are {"email": ..., "name": ...}
        if isinstance(address, dict):
            return formataddr((address.get("name") or "", address.get("email") or ""))
        return address or None

    @classmethod
    def _format_addresses(cls, addresses):
        return ", ".join(cls._format_address(a) or "" for a in addresses or []) or None

    @staticmethod
    def _make_attachment(attachment):
        if "content" in attachment:
            content = attachment["content"]
        else:
            # Large attachments come as a short-lived download URL instead.
            response = requests.get(attachment["url"], timeout=30)
            response.raise_for_status()
            content = response.content
        return AnymailInboundMessage.construct_attachment(
            content_type=attachment.get("content_type") or "application/octet-stream",
            content=content,
            filename=attachment.get("filename"),
            content_id=attachment.get("content_id"),
            base64="content" in attachment,
        )
