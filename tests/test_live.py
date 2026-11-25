"""
Live integration test against the real Lettermint API. Skipped unless
LETTERMINT_TEST_TOKEN is set. Use a Sandbox project token: sandbox_result makes
Lettermint simulate the delivery instead of sending.

    LETTERMINT_TEST_TOKEN=... LETTERMINT_TEST_FROM=you@your-verified-domain \
        uv run pytest -m live
"""

import os

import pytest
from anymail.message import AnymailMessage
from django.test import override_settings

TOKEN = os.environ.get("LETTERMINT_TEST_TOKEN")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not TOKEN, reason="set LETTERMINT_TEST_TOKEN to run live tests"),
]


@override_settings(ANYMAIL={"LETTERMINT_API_TOKEN": TOKEN})
def test_send_to_sandbox():
    msg = AnymailMessage(
        "django-anymail-lettermint live test",
        "Sent by the live integration test.",
        os.environ.get("LETTERMINT_TEST_FROM", "test@example.com"),
        [os.environ.get("LETTERMINT_TEST_TO", "delivered@example.com")],
        tags=["anymail-live-test"],
        metadata={"suite": "live"},
        esp_extra={"sandbox_result": "delivered"},
    )
    assert msg.send() == 1
    assert msg.anymail_status.status == {"queued"}
    assert msg.anymail_status.message_id
