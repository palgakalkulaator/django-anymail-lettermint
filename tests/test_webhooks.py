"""Tests for the Lettermint tracking and inbound webhooks, with signed payloads."""

import hashlib
import hmac
import json
import time
from base64 import b64encode
from datetime import datetime, timezone
from unittest import mock

import pytest
from anymail.exceptions import AnymailConfigurationError
from anymail.signals import inbound, tracking
from django.test import override_settings

from anymail_lettermint.webhooks import LettermintTrackingWebhookView

TRACKING_URL = "/anymail/lettermint/tracking/"
INBOUND_URL = "/anymail/lettermint/inbound/"


def sign(body: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def post(client, url, payload, secret="whsec_tracking", timestamp=None, signature=None, **headers):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    timestamp = int(time.time()) if timestamp is None else timestamp
    request_headers = {
        "X-Lettermint-Signature": signature or sign(body, secret, timestamp),
        "X-Lettermint-Delivery": str(timestamp),
        "X-Lettermint-Event": payload.get("event", "") if isinstance(payload, dict) else "",
        "X-Lettermint-Attempt": "1",
        **headers,
    }
    return client.post(url, data=body, content_type="application/json", headers=request_headers)


@pytest.fixture
def tracking_handler():
    handler = mock.Mock()
    tracking.connect(handler)
    yield handler
    tracking.disconnect(handler)


@pytest.fixture
def inbound_handler():
    handler = mock.Mock()
    inbound.connect(handler)
    yield handler
    inbound.disconnect(handler)


def message_event(event, **data):
    return {
        "id": "4f7b4b52-0bd0-4f2c-9d0b-2a3c3f6c1e11",
        "event": event,
        "timestamp": "2026-11-25T10:15:30.000Z",
        "sandbox": False,
        "data": {
            "message_id": "msg-123",
            "subject": "Welcome",
            "recipient": "to@example.com",
            "metadata": {"user_id": "42"},
            "headers": [],
            "tag": "welcome",
            "tags": [{"name": "__lettermint_tag", "value": "welcome"}],
            **data,
        },
    }


def received_event(handler):
    assert handler.call_count == 1
    kwargs = handler.call_args.kwargs
    assert kwargs["sender"] is LettermintTrackingWebhookView
    assert kwargs["esp_name"] == "Lettermint"
    return kwargs["event"]


def test_delivered(client, tracking_handler):
    payload = message_event(
        "message.delivered",
        response={"status_code": 250, "enhanced_status_code": "2.0.0", "content": "250 2.0.0 OK"},
    )
    response = post(client, TRACKING_URL, payload)
    assert response.status_code == 200
    event = received_event(tracking_handler)
    assert event.event_type == "delivered"
    assert event.event_id == "4f7b4b52-0bd0-4f2c-9d0b-2a3c3f6c1e11"
    assert event.timestamp == datetime(2026, 11, 25, 10, 15, 30, tzinfo=timezone.utc)
    assert event.message_id == "msg-123"
    assert event.recipient == "to@example.com"
    assert event.metadata == {"user_id": "42"}
    assert event.tags == ["welcome"]
    assert event.mta_response == "250 2.0.0 OK"
    assert event.reject_reason is None
    assert event.esp_event == payload


@pytest.mark.parametrize(
    ("esp_type", "event_type", "reject_reason"),
    [
        ("message.created", "queued", None),
        ("message.scheduled", "queued", None),
        ("message.sent", "sent", None),
        ("message.delivered", "delivered", None),
        ("message.auto_replied", "autoresponded", None),
        ("message.hard_bounced", "bounced", "bounced"),
        ("message.soft_bounced", "deferred", None),
        ("message.spam_complaint", "complained", "spam"),
        ("message.failed", "failed", "other"),
        ("message.suppressed", "rejected", "blocked"),
        ("message.policy_rejected", "rejected", "spam"),
        ("message.unsubscribed", "unsubscribed", "unsubscribed"),
        ("message.opened", "opened", None),
        ("message.clicked", "clicked", None),
        ("message.canceled", "unknown", None),
        ("message.rescheduled", "unknown", None),
        ("suppression.added", "unknown", None),
    ],
)
def test_event_type_mapping(client, tracking_handler, esp_type, event_type, reject_reason):
    post(client, TRACKING_URL, message_event(esp_type))
    event = received_event(tracking_handler)
    assert event.event_type == event_type
    assert event.reject_reason == reject_reason


def test_hard_bounce(client, tracking_handler):
    payload = message_event(
        "message.hard_bounced",
        response={
            "status_code": 550,
            "enhanced_status_code": "5.1.1",
            "content": "550 5.1.1 User unknown",
        },
    )
    post(client, TRACKING_URL, payload)
    event = received_event(tracking_handler)
    assert event.event_type == "bounced"
    assert event.mta_response == "550 5.1.1 User unknown"


def test_failed_reason(client, tracking_handler):
    payload = message_event(
        "message.failed",
        reason="TLS could not be negotiated",
        reason_code="enforced_tls_failed",
        response={"status_code": None},
    )
    post(client, TRACKING_URL, payload)
    event = received_event(tracking_handler)
    assert event.event_type == "failed"
    assert event.description == "TLS could not be negotiated"
    assert event.mta_response is None


def test_clicked(client, tracking_handler):
    payload = message_event(
        "message.clicked",
        destination_url="https://example.com/welcome",
        user_agent="Mozilla/5.0",
        clicked_at="2026-11-25T10:15:30.000Z",
    )
    post(client, TRACKING_URL, payload)
    event = received_event(tracking_handler)
    assert event.event_type == "clicked"
    assert event.click_url == "https://example.com/welcome"
    assert event.user_agent == "Mozilla/5.0"


def test_created_has_no_recipient(client, tracking_handler):
    payload = message_event("message.created", to=["a@example.com", "b@example.com"])
    del payload["data"]["recipient"]
    post(client, TRACKING_URL, payload)
    assert received_event(tracking_handler).recipient is None


def test_webhook_test_event(client, tracking_handler):
    payload = {
        "id": "d-1",
        "event": "webhook.test",
        "timestamp": "2026-11-25T10:15:30Z",
        "data": {"message": "Test", "webhook_id": "wh-1", "timestamp": 1764065730},
    }
    assert post(client, TRACKING_URL, payload).status_code == 200
    event = received_event(tracking_handler)
    assert event.event_type == "unknown"
    assert event.message_id is None
    assert event.tags == []
    assert event.metadata == {}


def test_missing_data_is_tolerated(client, tracking_handler):
    post(client, TRACKING_URL, {"event": "message.sent", "data": None, "timestamp": "junk"})
    event = received_event(tracking_handler)
    assert event.event_type == "sent"
    assert event.timestamp is None


def test_multiple_signatures_any_may_match(client, tracking_handler):
    payload = message_event("message.sent")
    body = json.dumps(payload).encode()
    now = int(time.time())
    good = sign(body, "whsec_tracking", now)
    signature = f"t={now},v1={'0' * 64},{good.split(',')[1]}"
    assert post(client, TRACKING_URL, body, timestamp=now, signature=signature).status_code == 200


@pytest.mark.parametrize(
    "case",
    ["bad_signature", "wrong_secret", "stale", "future", "delivery_mismatch", "tampered_body"],
)
def test_rejects_invalid_signature(client, tracking_handler, case):
    payload = message_event("message.delivered")
    body = json.dumps(payload).encode()
    now = int(time.time())
    kwargs = {}
    if case == "bad_signature":
        kwargs["signature"] = f"t={now},v1={'a' * 64}"
    elif case == "wrong_secret":
        kwargs["secret"] = "whsec_other"
    elif case == "stale":
        kwargs["timestamp"] = now - 301
    elif case == "future":
        kwargs["timestamp"] = now + 301
    elif case == "delivery_mismatch":
        kwargs["signature"] = sign(body, "whsec_tracking", now)
        kwargs["X-Lettermint-Delivery"] = str(now - 1)
    elif case == "tampered_body":
        kwargs["signature"] = sign(body, "whsec_tracking", now)
        body = body.replace(b"to@example.com", b"evil@example.com")
    kwargs.setdefault("timestamp", now)
    response = post(client, TRACKING_URL, body, **kwargs)
    assert response.status_code == 400
    tracking_handler.assert_not_called()


def test_timestamp_at_tolerance_is_accepted(client, tracking_handler):
    now = int(time.time())
    response = post(client, TRACKING_URL, message_event("message.sent"), timestamp=now - 299)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "signature",
    [
        None,  # header missing
        "v1=" + "a" * 64,  # no timestamp
        "t=1700000000",  # no v1 signature
        "t=abc,v1=" + "a" * 64,  # non-numeric timestamp
        "t=1700000000,v1=nothex",
    ],
)
def test_rejects_unsigned_or_malformed(client, tracking_handler, signature):
    body = json.dumps(message_event("message.sent")).encode()
    headers = {"X-Lettermint-Delivery": "1700000000"}
    if signature:
        headers["X-Lettermint-Signature"] = signature
    response = client.post(
        TRACKING_URL, data=body, content_type="application/json", headers=headers
    )
    assert response.status_code == 400
    tracking_handler.assert_not_called()


def test_rejects_signed_non_object_json(client, tracking_handler):
    assert post(client, TRACKING_URL, b"[1, 2]").status_code == 400
    assert post(client, TRACKING_URL, b"not json").status_code == 400
    tracking_handler.assert_not_called()


@override_settings(ANYMAIL={"LETTERMINT_API_TOKEN": "t"})
def test_secret_is_required():
    with pytest.raises(AnymailConfigurationError, match="LETTERMINT_WEBHOOK_SECRET"):
        LettermintTrackingWebhookView.as_view()(mock.Mock())


def test_secret_as_view_kwarg(rf, tracking_handler):
    view = LettermintTrackingWebhookView.as_view(webhook_secret="whsec_kwarg")
    body = json.dumps(message_event("message.sent")).encode()
    now = int(time.time())
    request = rf.post(
        TRACKING_URL,
        data=body,
        content_type="application/json",
        headers={
            "X-Lettermint-Signature": sign(body, "whsec_kwarg", now),
            "X-Lettermint-Delivery": str(now),
        },
    )
    assert view(request).status_code == 200


@override_settings(
    ANYMAIL={
        "LETTERMINT_WEBHOOK_SECRET": "whsec_tracking",
        "WEBHOOK_SECRET": "user:pass",
    }
)
def test_basic_auth_also_enforced_when_configured(client, tracking_handler):
    assert post(client, TRACKING_URL, message_event("message.sent")).status_code == 400
    auth = "Basic " + b64encode(b"user:pass").decode()
    response = post(client, TRACKING_URL, message_event("message.sent"), Authorization=auth)
    assert response.status_code == 200


def inbound_payload(**data):
    return {
        "id": "8b1d0b0e-1f7e-4c55-8a3d-1c9b1a2e3f40",
        "event": "message.inbound",
        "timestamp": "2026-11-25T10:15:30.000Z",
        "data": {
            "route": "inbound",
            "message_id": "in-1",
            "envelope": {
                "remote_ip": "203.0.113.5",
                "remote_hostname": "mx.example.net",
                "helo": "mx.example.net",
                "mail_from": "bounces@example.net",
            },
            "from": {"email": "sender@example.net", "name": "Sender Näme", "subaddress": None},
            "to": [{"email": "support@example.com", "name": "", "subaddress": None}],
            "cc": [{"email": "cc@example.com", "name": "CC", "subaddress": None}],
            "recipient": "support+ticket-9@example.com",
            "subaddress": "ticket-9",
            "reply_to": [],
            "subject": "Help",
            "date": "Tue, 25 Nov 2026 10:15:00 +0000",
            "body": {"text": "Plain body", "html": "<p>HTML body</p>"},
            "tag": None,
            "tags": [],
            "headers": [{"name": "X-Mailer", "value": "Thunderbird"}],
            "attachments": [
                {
                    "filename": "note.txt",
                    "content_type": "text/plain",
                    "size": 5,
                    "content_id": None,
                    "content": b64encode(b"hello").decode(),
                }
            ],
            "is_spam": False,
            "spam_score": 1.5,
            "spam_symbols": [],
            **data,
        },
    }


def test_inbound(client, inbound_handler):
    payload = inbound_payload()
    response = post(client, INBOUND_URL, payload, secret="whsec_inbound")
    assert response.status_code == 200
    kwargs = inbound_handler.call_args.kwargs
    assert kwargs["esp_name"] == "Lettermint"
    event = kwargs["event"]
    assert event.event_type == "inbound"
    assert event.event_id == "8b1d0b0e-1f7e-4c55-8a3d-1c9b1a2e3f40"
    assert event.esp_event == payload
    message = event.message
    assert message.from_email.display_name == "Sender Näme"
    assert message.from_email.addr_spec == "sender@example.net"
    assert [a.addr_spec for a in message.to] == ["support@example.com"]
    assert [a.addr_spec for a in message.cc] == ["cc@example.com"]
    assert message.subject == "Help"
    assert message.text == "Plain body"
    assert message.html == "<p>HTML body</p>"
    assert message["X-Mailer"] == "Thunderbird"
    assert message.envelope_sender == "bounces@example.net"
    assert message.envelope_recipient == "support+ticket-9@example.com"
    assert message.spam_detected is False
    assert message.spam_score == 1.5
    [attachment] = message.attachments
    assert attachment.get_filename() == "note.txt"
    assert attachment.get_content_bytes() == b"hello"


def test_inbound_url_attachment_and_inline(client, inbound_handler):
    payload = inbound_payload(
        attachments=[
            {
                "filename": "big.pdf",
                "content_type": "application/pdf",
                "size": 9,
                "content_id": None,
                "url": "https://files.lettermint.co/att/1?sig=x",
                "expires_at": "2026-11-26T10:15:30Z",
            },
            {
                "filename": "logo.png",
                "content_type": "image/png",
                "size": 3,
                "content_id": "logo@example.net",
                "content": b64encode(b"PNG").decode(),
            },
        ]
    )
    download = mock.Mock(content=b"%PDF-1.7")
    with mock.patch("anymail_lettermint.webhooks.requests.get", return_value=download) as get:
        post(client, INBOUND_URL, payload, secret="whsec_inbound")
    get.assert_called_once_with("https://files.lettermint.co/att/1?sig=x", timeout=30)
    download.raise_for_status.assert_called_once_with()
    message = inbound_handler.call_args.kwargs["event"].message
    [pdf] = message.attachments
    assert pdf.get_content_bytes() == b"%PDF-1.7"
    inline = message.content_id_map["logo@example.net"]
    assert inline.get_content_bytes() == b"PNG"


def test_inbound_uses_its_own_secret(client, inbound_handler):
    response = post(client, INBOUND_URL, inbound_payload(), secret="whsec_tracking")
    assert response.status_code == 400
    inbound_handler.assert_not_called()


def test_inbound_rejects_tracking_event(client, inbound_handler):
    with pytest.raises(AnymailConfigurationError, match="message.delivered"):
        post(client, INBOUND_URL, message_event("message.delivered"), secret="whsec_inbound")
    inbound_handler.assert_not_called()
