"""Tests for the Lettermint Anymail EmailBackend (API calls are mocked)."""

from base64 import b64encode
from datetime import datetime, timezone
from email.mime.image import MIMEImage

import django
import pytest
from anymail.exceptions import AnymailAPIError, AnymailUnsupportedFeature
from anymail.message import AnymailMessage
from django.core import mail
from django.test import override_settings

from .conftest import make_response, sent


def test_simple_send(mock_request):
    msg = mail.EmailMessage(
        "Subject",
        "Body text",
        "Sender <from@example.com>",
        ["to@example.com", "Other <to2@example.com>"],
        cc=["cc@example.com"],
        bcc=["bcc@example.com"],
        reply_to=["reply@example.com"],
        headers={"X-Custom": 42},
    )
    assert msg.send() == 1
    method, url, headers, data = sent(mock_request)
    assert method == "POST"
    assert url == "https://api.lettermint.co/v1/send"
    assert headers["x-lettermint-token"] == "test-token"
    assert "Idempotency-Key" not in headers
    assert data == {
        "from": "Sender <from@example.com>",
        "to": ["to@example.com", "Other <to2@example.com>"],
        "cc": ["cc@example.com"],
        "bcc": ["bcc@example.com"],
        "reply_to": ["reply@example.com"],
        "subject": "Subject",
        "text": "Body text",
        "headers": {"X-Custom": "42"},
    }
    assert msg.anymail_status.message_id == "msg-1"
    assert msg.anymail_status.status == {"queued"}
    assert msg.anymail_status.recipients["bcc@example.com"].message_id == "msg-1"


def test_html_and_attachments(mock_request):
    msg = mail.EmailMultiAlternatives("S", "text", "from@example.com", ["to@example.com"])
    msg.attach_alternative("<p>html</p>", "text/html")
    msg.attach("report.pdf", b"%PDF-1.4", "application/pdf")
    img = MIMEImage(b"GIF89a", "gif")
    img.add_header("Content-ID", "<logo>")
    img.add_header("Content-Disposition", "inline")
    msg.attach(img)
    msg.send()
    data = sent(mock_request)[3]
    assert data["html"] == "<p>html</p>"
    assert data["attachments"][0] == {
        "filename": "report.pdf",
        "content": b64encode(b"%PDF-1.4").decode(),
        "content_type": "application/pdf",
    }
    inline = data["attachments"][1]
    assert inline["content_id"] == "logo"
    assert inline["filename"] == "attachment.gif"


def test_anymail_features(mock_request):
    msg = AnymailMessage(
        "S",
        "text",
        "from@example.com",
        ["to@example.com"],
        metadata={"user_id": 123, "plan": "pro"},
        tags=["welcome"],
        track_clicks=False,
        track_opens=True,
        send_at=datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc),
        esp_extra={"route": "broadcast", "settings": {"tls": "enforced"}},
    )
    msg.send()
    data = sent(mock_request)[3]
    assert data["metadata"] == {"user_id": "123", "plan": "pro"}
    assert data["tag"] == "welcome"
    assert data["settings"] == {"track_clicks": False, "track_opens": True, "tls": "enforced"}
    assert data["scheduled_at"] == "2026-12-01T09:00:00+00:00"
    assert data["route"] == "broadcast"


@override_settings(ANYMAIL={"LETTERMINT_API_TOKEN": "t", "LETTERMINT_ROUTE": "outgoing"})
def test_default_route_setting(mock_request):
    mail.send_mail("S", "text", "from@example.com", ["to@example.com"])
    assert sent(mock_request)[3]["route"] == "outgoing"


def test_unsupported_features(mock_request):
    msg = AnymailMessage("S", "t", "from@example.com", ["to@example.com"], tags=["a", "b"])
    with pytest.raises(AnymailUnsupportedFeature, match="multiple tags"):
        msg.send()
    msg = AnymailMessage("S", "t", "from@example.com", ["to@example.com"], template_id="x")
    with pytest.raises(AnymailUnsupportedFeature):
        msg.send()
    msg = AnymailMessage(
        "S", "t", "from@example.com", ["to@example.com"], merge_data={"to@example.com": {"a": 1}}
    )
    with pytest.raises(AnymailUnsupportedFeature, match="merge_data"):
        msg.send()
    msg = mail.EmailMultiAlternatives("S", "t", "from@example.com", ["to@example.com"])
    msg.attach_alternative("<p>1</p>", "text/html")
    msg.attach_alternative("<p>2</p>", "text/html")
    with pytest.raises(AnymailUnsupportedFeature, match="multiple html parts"):
        msg.send()
    mock_request.assert_not_called()


def test_inline_attachment_without_cid(mock_request):
    msg = mail.EmailMessage("S", "t", "from@example.com", ["to@example.com"])
    img = MIMEImage(b"GIF89a", "gif")
    img.add_header("Content-Disposition", "inline")
    msg.attach(img)
    with pytest.raises(AnymailUnsupportedFeature, match="without Content-ID"):
        msg.send()


def test_batch_send(mock_request):
    mock_request.return_value = make_response(
        body=[
            {"message_id": "m-a", "status": "pending"},
            {"message_id": "m-b", "status": "scheduled", "scheduled_at": "x"},
        ]
    )
    msg = AnymailMessage(
        "S",
        "t",
        "from@example.com",
        ["a@example.com", "B <b@example.com>"],
        cc=["cc@example.com"],
        merge_data={},  # Anymail's "send separately to each recipient"
        metadata={"campaign": "c1"},
        merge_metadata={"a@example.com": {"user": 1}},
        merge_headers={"b@example.com": {"List-Unsubscribe": "<https://x/b>"}},
    )
    msg.send()
    _, url, _, data = sent(mock_request)
    assert url == "https://api.lettermint.co/v1/send/batch"
    assert [item["to"] for item in data] == [["a@example.com"], ["B <b@example.com>"]]
    assert data[0]["metadata"] == {"campaign": "c1", "user": "1"}
    assert data[1]["metadata"] == {"campaign": "c1"}
    assert data[1]["headers"] == {"List-Unsubscribe": "<https://x/b>"}
    assert "headers" not in data[0]
    assert msg.anymail_status.recipients["a@example.com"].message_id == "m-a"
    assert msg.anymail_status.recipients["b@example.com"].message_id == "m-b"
    # cc goes on every batch message and reports the first message's id
    assert msg.anymail_status.recipients["cc@example.com"].message_id == "m-a"


def test_batch_response_length_mismatch(mock_request):
    mock_request.return_value = make_response(body=[{"message_id": "m-a", "status": "pending"}])
    msg = AnymailMessage(
        "S", "t", "from@example.com", ["a@example.com", "b@example.com"], merge_data={}
    )
    with pytest.raises(AnymailAPIError, match="Invalid Lettermint API response"):
        msg.send()


def test_idempotency_key(mock_request):
    msg = AnymailMessage(
        "S", "t", "from@example.com", ["to@example.com"], esp_extra={"idempotency_key": "order-42"}
    )
    msg.send()
    _, _, headers, data = sent(mock_request)
    assert headers["Idempotency-Key"] == "order-42"
    assert "idempotency_key" not in data  # a header, not a body field


def test_idempotency_key_batch(mock_request):
    mock_request.return_value = make_response(body=[{"message_id": "m-a", "status": "pending"}])
    msg = AnymailMessage(
        "S",
        "t",
        "from@example.com",
        ["a@example.com"],
        merge_data={},
        esp_extra={"idempotency_key": "newsletter-7"},
    )
    msg.send()
    _, url, headers, data = sent(mock_request)
    assert url.endswith("/send/batch")
    assert headers["Idempotency-Key"] == "newsletter-7"
    assert "idempotency_key" not in data[0]


def test_api_error(mock_request):
    mock_request.return_value = make_response(422, {"message": "Invalid from domain"})
    with pytest.raises(AnymailAPIError, match="Invalid from domain"):
        mail.send_mail("S", "t", "from@example.com", ["to@example.com"])


def test_idempotency_conflict_is_api_error(mock_request):
    mock_request.return_value = make_response(409, {"message": "concurrent_idempotent_requests"})
    with pytest.raises(AnymailAPIError, match="concurrent_idempotent_requests"):
        mail.send_mail("S", "t", "from@example.com", ["to@example.com"])


def test_fail_silently(mock_request):
    mock_request.return_value = make_response(500, {"message": "boom"})
    assert mail.send_mail("S", "t", "from@example.com", ["to@example.com"], fail_silently=True) == 0


def test_invalid_response(mock_request):
    mock_request.return_value = make_response(body={"unexpected": True})
    with pytest.raises(AnymailAPIError, match="Invalid Lettermint API response"):
        mail.send_mail("S", "t", "from@example.com", ["to@example.com"])


@override_settings(
    ANYMAIL={"LETTERMINT_API_TOKEN": "t", "LETTERMINT_API_URL": "https://eu.example.test/v1"}
)
def test_api_url_setting_adds_trailing_slash(mock_request):
    mail.send_mail("S", "t", "from@example.com", ["to@example.com"])
    assert sent(mock_request)[1] == "https://eu.example.test/v1/send"


@pytest.mark.skipif(django.VERSION < (6, 1), reason="MAILERS is new in Django 6.1")
def test_mailers_options(mock_request, settings):
    del settings.EMAIL_BACKEND
    settings.ANYMAIL = {}
    settings.MAILERS = {
        "default": {
            "BACKEND": "anymail_lettermint.EmailBackend",
            "OPTIONS": {"api_token": "from-options", "route": "transactional"},
        }
    }
    mail.send_mail("S", "t", "from@example.com", ["to@example.com"])
    _, _, headers, data = sent(mock_request)
    assert headers["x-lettermint-token"] == "from-options"
    assert data["route"] == "transactional"
