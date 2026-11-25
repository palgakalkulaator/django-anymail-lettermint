import json
from unittest import mock

import pytest
import requests


def make_response(status=202, body=None):
    response = requests.Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = json.dumps(
        body if body is not None else {"message_id": "msg-1", "status": "pending"}
    ).encode()
    response.url = "https://api.lettermint.co/v1/send"
    return response


@pytest.fixture
def mock_request():
    with mock.patch("requests.Session.request", autospec=True) as m:
        m.return_value = make_response()
        yield m


def sent(mock_request):
    """(method, url, headers, json body) of the last API call"""
    call = mock_request.call_args
    bound = dict(zip(["self", "method", "url"], call.args, strict=False), **call.kwargs)
    return bound["method"], bound["url"], bound["headers"], json.loads(bound["data"])


@pytest.fixture(autouse=True)
def lettermint_email_backend(settings):
    # pytest-django switches to the locmem backend; send through ours instead.
    settings.EMAIL_BACKEND = "anymail_lettermint.EmailBackend"
