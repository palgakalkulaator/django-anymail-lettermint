"""
Live integration tests against the real Lettermint API. Skipped unless
LETTERMINT_TEST_TOKEN is set. Use a Sandbox project token: sandbox_result makes
Lettermint simulate the delivery instead of sending.

    uv run --env-file .env pytest -m live -v
"""

import base64
import os
import uuid
from datetime import timedelta

import pytest
from anymail.exceptions import AnymailAPIError
from anymail.message import AnymailMessage, attach_inline_image
from django.utils import timezone

TOKEN = os.environ.get("LETTERMINT_TEST_TOKEN")
FROM = os.environ.get("LETTERMINT_TEST_FROM", "test@example.com")
TO = os.environ.get("LETTERMINT_TEST_TO", "delivered@example.com")

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not TOKEN, reason="set LETTERMINT_TEST_TOKEN to run live tests"),
]


@pytest.fixture(autouse=True)
def live_token(settings):
    settings.ANYMAIL = {"LETTERMINT_API_TOKEN": TOKEN}


def message(subject, **kwargs):
    kwargs.setdefault("to", [TO])
    esp_extra = {"sandbox_result": "delivered", **kwargs.pop("esp_extra", {})}
    return AnymailMessage(
        f"django-anymail-lettermint live: {subject}",
        "Sent by the live integration tests.",
        FROM,
        esp_extra=esp_extra,
        **kwargs,
    )


def assert_queued(msg):
    assert msg.anymail_status.status == {"queued"}, msg.anymail_status.esp_response.text
    assert msg.anymail_status.message_id


def test_plain_send():
    """Text body, one tag and metadata are accepted; the send is queued."""
    msg = message("plain", tags=["anymail-live-test"], metadata={"suite": "live", "n": 1})
    assert msg.send() == 1
    assert_queued(msg)


def test_html_attachment_and_inline_image():
    """HTML with a cid: inline image plus regular attachments are accepted."""
    msg = message("attachments")
    cid = attach_inline_image(msg, PNG, filename="dot.png")
    msg.attach_alternative(f'<p>Inline image: <img src="cid:{cid}"></p>', "text/html")
    msg.attach("notes.txt", "plain text attachment", "text/plain")
    msg.attach("invite.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "text/calendar")
    assert msg.send() == 1
    assert_queued(msg)


def test_tracking_settings_and_name_value_tags():
    """track_opens/clicks, TLS policy, extra headers and name/value tags are accepted."""
    msg = message(
        "settings",
        headers={"X-Live-Test": "1"},
        track_opens=True,
        track_clicks=False,
        esp_extra={
            "settings": {"tls": "opportunistic"},
            "tags": [{"name": "suite", "value": "live"}],
        },
    )
    msg.attach_alternative('<p><a href="https://example.com">link</a></p>', "text/html")
    assert msg.send() == 1
    assert_queued(msg)


def test_batch_send_with_merge_metadata():
    """merge_data={} goes through /send/batch: one queued message id per "to"."""
    local, _, domain = TO.partition("@")
    second = f"{local}+batch@{domain}"
    msg = message(
        "batch",
        to=[TO, second],
        merge_data={},
        metadata={"suite": "live"},
        merge_metadata={TO: {"row": "1"}, second: {"row": "2"}},
    )
    assert msg.send() == 1
    recipients = msg.anymail_status.recipients
    assert recipients[TO].status == "queued"
    assert recipients[second].status == "queued"
    assert recipients[TO].message_id != recipients[second].message_id


def test_send_at():
    """send_at becomes scheduled_at; Lettermint answers "scheduled" (queued)."""
    msg = message("scheduled", send_at=timezone.now() + timedelta(hours=1))
    try:
        msg.send()
    except AnymailAPIError as err:
        if "FEATURE_NOT_AVAILABLE" in str(err):
            pytest.skip("scheduled sending needs a Growth or Pro plan")
        raise
    assert_queued(msg)
    assert msg.anymail_status.esp_response.json()["status"] == "scheduled"


def test_idempotency_key_replay():
    """The same Idempotency-Key and body twice returns the same message id."""
    key = f"anymail-live-{uuid.uuid4()}"
    first = message("idempotency", esp_extra={"idempotency_key": key})
    first.send()
    second = message("idempotency", esp_extra={"idempotency_key": key})
    second.send()
    assert second.anymail_status.message_id == first.anymail_status.message_id
