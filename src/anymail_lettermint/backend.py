"""
Lettermint (lettermint.co) email backend for Django, built on django-anymail.

Lettermint is not (yet) one of Anymail's built-in ESPs, so this module follows
the same architecture Anymail uses for its own HTTP-API backends (see
anymail/backends/resend.py and anymail/backends/scaleway.py):

* ``EmailBackend`` subclasses ``AnymailRequestsBackend``: it reads settings,
  owns the pooled ``requests.Session``, and parses the API response into
  Anymail's per-recipient ``message.anymail_status``.
* ``LettermintPayload`` subclasses ``RequestsPayload``: Anymail normalizes the
  Django ``EmailMessage`` (addresses, bodies, attachments, headers, tags,
  metadata, send_at, merge_* ...) and calls one ``set_<attr>()`` hook per
  feature; each hook maps it onto Lettermint's JSON wire format.

Anything Lettermint can't do raises ``AnymailUnsupportedFeature`` instead of
being silently dropped (unless ANYMAIL["IGNORE_UNSUPPORTED_FEATURES"] = True).

API reference: POST https://api.lettermint.co/v1/send   (single message)
               POST https://api.lettermint.co/v1/send/batch  (list of messages)
Auth header:   x-lettermint-token: <project sending token>
"""

import contextlib
import mimetypes

from anymail.backends.base_requests import AnymailRequestsBackend, RequestsPayload
from anymail.exceptions import AnymailRequestsAPIError
from anymail.message import AnymailRecipientStatus
from anymail.utils import CaseInsensitiveCasePreservingDict, get_anymail_setting

__all__ = ["EmailBackend", "LettermintPayload"]


class EmailBackend(AnymailRequestsBackend):
    """
    Lettermint (lettermint.co) API Email Backend
    """

    esp_name = "Lettermint"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        esp_name = self.esp_name
        # allow_bare=True also accepts a top-level LETTERMINT_API_TOKEN setting
        self.api_token = get_anymail_setting(
            "api_token", esp_name=esp_name, kwargs=kwargs, allow_bare=True
        )
        # Optional default route slug, applied to every message
        # (override per message with esp_extra={"route": "..."}).
        self.route = get_anymail_setting("route", esp_name=esp_name, kwargs=kwargs, default=None)
        api_url = get_anymail_setting(
            "api_url",
            esp_name=esp_name,
            kwargs=kwargs,
            default="https://api.lettermint.co/v1/",
        )
        if not api_url.endswith("/"):
            api_url += "/"
        super().__init__(api_url, **kwargs)

    def build_message_payload(self, message, defaults):
        return LettermintPayload(message, defaults, self)

    # Lettermint send status -> Anymail status.
    # The send API only reports acceptance; delivery, bounces, etc. arrive
    # later through Lettermint's webhooks.
    _recipient_status_map = {
        "pending": "queued",
        "scheduled": "queued",
    }

    def _status_for(self, item):
        message_id = item["message_id"]
        status = self._recipient_status_map.get(item.get("status"), "unknown")
        return AnymailRecipientStatus(message_id=message_id, status=status)

    def parse_recipient_status(self, response, payload, message):
        parsed_response = self.deserialize_json_response(response, payload, message)
        try:
            if payload.is_batch():
                # /send/batch returns a list of results, in the same order as
                # the messages we sent (one per "to" recipient).
                batch_statuses = [self._status_for(item) for item in parsed_response]
                if len(batch_statuses) != len(payload.to_recipients):
                    raise ValueError("batch response length mismatch")
            else:
                single_status = self._status_for(parsed_response)
        except (KeyError, TypeError, ValueError) as err:
            raise AnymailRequestsAPIError(
                "Invalid Lettermint API response format",
                email_message=message,
                payload=payload,
                response=response,
                backend=self,
            ) from err

        if payload.is_batch():
            # cc/bcc are copied onto every batch message; report them with the
            # first message's id (as Anymail's Resend backend does).
            first = batch_statuses[0] if batch_statuses else None
            recipient_status = CaseInsensitiveCasePreservingDict(
                {recip.addr_spec: first for recip in payload.recipients}
            )
            for recip, status in zip(payload.to_recipients, batch_statuses, strict=True):
                recipient_status[recip.addr_spec] = status
        else:
            recipient_status = CaseInsensitiveCasePreservingDict(
                {recip.addr_spec: single_status for recip in payload.recipients}
            )
        return dict(recipient_status)


class LettermintPayload(RequestsPayload):
    def __init__(self, message, defaults, backend, *args, **kwargs):
        self.recipients = []  # all to/cc/bcc, for parse_recipient_status
        self.to_recipients = []  # "to" only, for batch send
        self.merge_metadata = {}
        self.merge_headers = {}
        headers = kwargs.pop("headers", {})
        headers["x-lettermint-token"] = backend.api_token
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        super().__init__(message, defaults, backend, *args, headers=headers, **kwargs)

    def get_api_endpoint(self):
        if self.is_batch():
            return "send/batch"
        return "send"

    #
    # Serialization
    #

    def serialize_data(self):
        if not self.is_batch():
            return self.serialize_json(self.data)

        # Batch send: one message per "to" recipient, so each recipient sees
        # only their own address and can get their own metadata/headers.
        to_emails = self.data.pop("to", [])
        batch = []
        for to_email, to in zip(to_emails, self.to_recipients, strict=True):
            item = dict(self.data)
            item["to"] = [to_email]
            if to.addr_spec in self.merge_metadata:
                metadata = dict(self.data.get("metadata", {}))
                metadata.update(self._stringify(self.merge_metadata[to.addr_spec]))
                item["metadata"] = metadata
            if to.addr_spec in self.merge_headers:
                headers = CaseInsensitiveCasePreservingDict(self.data.get("headers", {}))
                headers.update(self._stringify(self.merge_headers[to.addr_spec]))
                item["headers"] = dict(headers)
            batch.append(item)
        return self.serialize_json(batch)

    @staticmethod
    def _stringify(mapping):
        # Lettermint requires string values for headers and metadata.
        return {str(k): str(v) for k, v in mapping.items()}

    #
    # Payload construction
    #

    def init_payload(self):
        self.data = {}  # becomes json
        if self.backend.route:
            self.data["route"] = self.backend.route

    def set_from_email(self, email):
        self.data["from"] = email.format(idna_encode=self.backend.idna_encode)

    def set_recipients(self, recipient_type, emails):
        assert recipient_type in ["to", "cc", "bcc"]
        if emails:
            self.data[recipient_type] = [
                email.format(idna_encode=self.backend.idna_encode) for email in emails
            ]
            self.recipients += emails
            if recipient_type == "to":
                self.to_recipients = emails

    def set_subject(self, subject):
        self.data["subject"] = subject

    def set_reply_to(self, emails):
        if emails:
            self.data["reply_to"] = [
                email.format(idna_encode=self.backend.idna_encode) for email in emails
            ]

    def set_extra_headers(self, headers):
        self.data.setdefault("headers", {}).update(self._stringify(headers))

    def set_text_body(self, body):
        self.data["text"] = body

    def set_html_body(self, body):
        if "html" in self.data:
            # A second html part can arrive via attach_alternative()
            self.unsupported_feature("multiple html parts")
        self.data["html"] = body

    def make_attachment(self, attachment):
        # Lettermint requires a filename; make a sensible one if missing.
        filename = attachment.name
        if not filename:
            ext = mimetypes.guess_extension(attachment.mimetype) or ".bin"
            filename = f"attachment{ext}"
        att = {
            "filename": filename,
            "content": attachment.b64content,
            # content_type keeps params, e.g. "text/calendar; method=REQUEST"
            "content_type": attachment.content_type,
        }
        if attachment.inline:
            if not attachment.cid:
                self.unsupported_feature("inline attachments without Content-ID")
            att["content_id"] = attachment.cid
        return att

    def set_attachments(self, attachments):
        if attachments:
            self.data["attachments"] = [self.make_attachment(a) for a in attachments]

    def set_metadata(self, metadata):
        # Lettermint stores metadata with the message (it is not added as
        # email headers) and returns it in webhook events.
        if metadata:
            self.data["metadata"] = self._stringify(metadata)

    def set_send_at(self, send_at):
        # Anymail has already converted send_at to an aware datetime.
        with contextlib.suppress(AttributeError):  # a pre-formatted string stays as is
            send_at = send_at.isoformat()
        self.data["scheduled_at"] = send_at

    def set_tags(self, tags):
        # Anymail tags are plain strings. Lettermint's single "tag" field is the
        # equivalent. (Lettermint's name/value "tags" have no Anymail equivalent;
        # send those with esp_extra={"tags": [{"name": ..., "value": ...}]}.)
        if tags:
            self.data["tag"] = tags[0]
            if len(tags) > 1:
                self.unsupported_feature(f"multiple tags ({tags!r})")

    def set_track_clicks(self, track_clicks):
        self.data.setdefault("settings", {})["track_clicks"] = track_clicks

    def set_track_opens(self, track_opens):
        self.data.setdefault("settings", {})["track_opens"] = track_opens

    # Lettermint has no server-side templates, so template_id and
    # merge_global_data stay unsupported (base class raises for them). If it adds
    # templates, implement set_template_id / set_merge_global_data and extend set_merge_data.

    def set_merge_data(self, merge_data):
        # Empty merge_data ({}) is Anymail's way of asking for a batch send
        # (one message per "to" recipient); actual merge fields need templates.
        if any(recipient_data for recipient_data in merge_data.values()):
            self.unsupported_feature("merge_data")

    def set_merge_metadata(self, merge_metadata):
        self.merge_metadata = merge_metadata  # applied in serialize_data

    def set_merge_headers(self, merge_headers):
        self.merge_headers = merge_headers  # applied in serialize_data

    def set_esp_extra(self, extra):
        # Raw Lettermint fields: route, tags, sandbox_result, settings, ...
        extra = dict(extra)
        # Sent as the Idempotency-Key request header, not in the JSON body. For a
        # batch send the key covers the whole request.
        idempotency_key = extra.pop("idempotency_key", None)
        if idempotency_key is not None:
            self.headers["Idempotency-Key"] = idempotency_key
        if "settings" in extra:
            self.data.setdefault("settings", {}).update(extra.pop("settings"))
        self.data.update(extra)
