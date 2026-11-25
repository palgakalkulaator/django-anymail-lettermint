# django-anymail-lettermint

[Lettermint](https://lettermint.co) (EU transactional email) support for
[django-anymail](https://github.com/anymail/django-anymail): an email backend plus
tracking and inbound webhooks, written against Anymail's own base classes.

Your code uses Anymail's normalized API (`anymail_status`, `metadata`, `tags`, `send_at`,
`esp_extra`, the `tracking` and `inbound` signals). Moving to or from another Anymail ESP is
a settings change.

## Install

```bash
pip install django-anymail-lettermint
```

Requires Python 3.10+, Django 5.2 / 6.0 / 6.1 and django-anymail 15.x.
(Anymail 15 itself requires Django 5.0 or newer, so Django 4.2 is not supported.)

## Settings

Django 6.1+ (`MAILERS`):

```python
MAILERS = {
    "default": {
        "BACKEND": "anymail_lettermint.EmailBackend",
        "OPTIONS": {
            "api_token": env("LETTERMINT_API_TOKEN"),  # project sending token
            # "route": "outgoing",                     # default route slug
            # "api_url": "https://api.lettermint.co/v1/",
        },
    },
}
```

Django 6.0 and earlier (`EMAIL_BACKEND`):

```python
EMAIL_BACKEND = "anymail_lettermint.EmailBackend"
ANYMAIL = {
    "LETTERMINT_API_TOKEN": env("LETTERMINT_API_TOKEN"),
    # "LETTERMINT_ROUTE": "outgoing",
}
```

| Setting | Required | Meaning |
|---|---|---|
| `LETTERMINT_API_TOKEN` / `api_token` | yes | Project sending token (`x-lettermint-token`) |
| `LETTERMINT_ROUTE` / `route` | no | Route slug for every message; override with `esp_extra={"route": ...}` |
| `LETTERMINT_API_URL` / `api_url` | no | Defaults to `https://api.lettermint.co/v1/` |
| `LETTERMINT_WEBHOOK_SECRET` | for tracking webhooks | Signing secret (`whsec_…`) of the tracking webhook |
| `LETTERMINT_INBOUND_SECRET` | for inbound webhooks | Signing secret (`whsec_…`) of the inbound webhook |

## Sending

```python
from anymail.message import AnymailMessage

msg = AnymailMessage(
    "Welcome",
    "Hi!",
    "Team <team@example.com>",
    ["user@example.com"],
    tags=["welcome"],
    metadata={"user_id": 42},
    esp_extra={
        "idempotency_key": f"welcome-{user.pk}",  # sent as the Idempotency-Key header
        "tags": [{"name": "plan", "value": "pro"}],  # Lettermint name/value tags
        "sandbox_result": "delivered",  # Sandbox projects only
    },
)
msg.send()
msg.anymail_status.message_id  # Lettermint message_id
msg.anymail_status.status  # {"queued"}: Lettermint answered "pending" or "scheduled"
```

`merge_data={}` sends one message per `to` recipient through `POST /send/batch`, with
`merge_metadata` and `merge_headers` applied to each recipient.

### Feature support

| Anymail feature | Lettermint | Notes |
|---|---|---|
| `from`, `to`, `cc`, `bcc`, `reply_to`, subject | ✅ | |
| Text and HTML body | ✅ | One HTML part only |
| Attachments | ✅ | Inline attachments need a Content-ID. Unnamed attachments get a filename like `attachment.gif` |
| Extra headers | ✅ | Values are sent as strings |
| `metadata` | ✅ | Values are sent as strings. Stored with the message and returned in webhooks; not added as email headers |
| `tags` | ⚠️ one tag | Maps to Lettermint's single `tag`; more than one raises `AnymailUnsupportedFeature` |
| Name/value tags | via `esp_extra` | `esp_extra={"tags": [{"name": ..., "value": ...}]}` |
| `track_opens`, `track_clicks` | ✅ | Per-message `settings` override of the route |
| `send_at` | ✅ | `scheduled_at` |
| Batch send (`merge_data={}`) | ✅ | `POST /send/batch`, one message per `to` |
| `merge_metadata`, `merge_headers` | ✅ | Batch send only |
| `template_id`, `merge_data` values, `merge_global_data` | ❌ | Lettermint has no stored templates |
| `envelope_sender` | ❌ | |
| Route, TLS policy, sandbox result | via `esp_extra` | `route`, `settings={"tls": "enforced"}`, `sandbox_result` |
| Idempotency | via `esp_extra` | `idempotency_key` becomes the `Idempotency-Key` header. For a batch it covers the whole request |
| Tracking webhook | ✅ | Signed (HMAC-SHA256) |
| Inbound webhook | ✅ | Signed (HMAC-SHA256) |

Lettermint's API has no stored templates (so no `template_id` or merge variables), and it
sets the envelope sender (Return-Path) per domain in DNS, not per message. For personalized
email, render each body in Django and send one message per recipient:

```python
from django.template.loader import render_to_string

for user in users:
    context = {"user": user}
    AnymailMessage(
        render_to_string("welcome/subject.txt", context).strip(),
        render_to_string("welcome/body.txt", context),
        to=[user.email],
        metadata={"user_id": user.pk},
    ).send()
```

Unsupported features raise `AnymailUnsupportedFeature` unless you set
`ANYMAIL["IGNORE_UNSUPPORTED_FEATURES"] = True`.

## Webhooks

```python
# urls.py
urlpatterns = [
    path("anymail/", include("anymail_lettermint.urls")),
]
```

This adds:

- `https://yoursite.example/anymail/lettermint/tracking/` for `message.*` status events
- `https://yoursite.example/anymail/lettermint/inbound/` for `message.inbound`

In the Lettermint dashboard, create one webhook for each URL. Copy each webhook's signing
secret into `LETTERMINT_WEBHOOK_SECRET` (tracking) or `LETTERMINT_INBOUND_SECRET` (inbound).
Each view checks `X-Lettermint-Signature` (HMAC-SHA256 over `"<t>." + body`, compared in
constant time). It also requires `X-Lettermint-Delivery` to equal the signed timestamp and the
timestamp to be within 5 minutes. Anything else gets HTTP 400 and no signal fires. If you
also set Anymail's shared `WEBHOOK_SECRET`, basic auth is enforced as well.

Then handle Anymail's standard signals:

```python
from anymail.signals import inbound, tracking
from django.dispatch import receiver


@receiver(tracking)
def handle_tracking(sender, event, esp_name, **kwargs):
    if event.event_type == "bounced":
        ...  # event.recipient, event.message_id, event.mta_response, event.metadata


@receiver(inbound)
def handle_inbound(sender, event, esp_name, **kwargs):
    message = event.message  # AnymailInboundMessage
    message.envelope_recipient  # Lettermint's data.recipient: route on this
```

### Event mapping

| Lettermint event | Anymail `event_type` | `reject_reason` |
|---|---|---|
| `message.created`, `message.scheduled` | `queued` | |
| `message.sent` | `sent` | |
| `message.delivered` | `delivered` | |
| `message.auto_replied` | `autoresponded` | |
| `message.hard_bounced` | `bounced` | `bounced` |
| `message.soft_bounced` | `deferred` | |
| `message.spam_complaint` | `complained` | `spam` |
| `message.failed` | `failed` | `other` |
| `message.suppressed` | `rejected` | `blocked` |
| `message.policy_rejected` | `rejected` | `spam` |
| `message.unsubscribed` | `unsubscribed` | `unsubscribed` |
| `message.opened` | `opened` | |
| `message.clicked` | `clicked` | |
| anything else (`message.canceled`, `suppression.*`, `webhook.test`, …) | `unknown` | |

Every event carries `event_id` (Lettermint's delivery `id`, which stays the same on retries,
so use it to de-duplicate), `timestamp`, `message_id`, `recipient`, `metadata`, `tags`
(the single `tag`), `description` (`reason`), `mta_response` (`response.content`),
`click_url` (`destination_url`), `user_agent`, and the raw payload as `esp_event`.
`message.created` covers every recipient, so its `recipient` is `None`. For
`message.auto_replied`, `recipient` is the auto-reply's sender (`data.auto_reply.sender`).

Inbound attachments that Lettermint sends as a download URL instead of inline content are
fetched while the webhook request is being handled.

## How this differs from other Lettermint packages

| Package | Built on | Notes |
|---|---|---|
| **django-anymail-lettermint** (this) | django-anymail | Anymail's message API, `anymail_status`, `tracking`/`inbound` signals; no Lettermint SDK needed |
| [django-lettermint](https://pypi.org/project/django-lettermint/) | Lettermint Python SDK | Plain Django email backend; Django 6.1+ |
| [lettermint-django](https://pypi.org/project/lettermint-django/) | Lettermint Python SDK | Plain Django email backend |

Pick this one if you already use Anymail, or want to be able to switch ESPs without code
changes.

## Upstreaming

The goal is to merge this into django-anymail itself. Under Anymail's
[policy for new ESPs](https://github.com/anymail/django-anymail/discussions/501), that
requires a "new ESP request" issue that collects enough community interest. That request is
[anymail/django-anymail#508](https://github.com/anymail/django-anymail/issues/508). If you
use Lettermint with Django and want it in Anymail, add a 👍 reaction to the issue itself (only
👍 on the top post counts; "+1" comments don't). The backend and webhook modules here mirror
Anymail's own layout so they can move upstream largely unchanged.

## Development

```bash
uv sync
uv run pytest --cov          # unit tests, ≥95% coverage enforced
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run tox -p auto           # Python 3.10–3.14 × Django 5.2/6.0/6.1, plus lowest Anymail
```

Live test against a Lettermint Sandbox project (skipped without a token):

```bash
uv run --env-file .env pytest -m live   # LETTERMINT_TEST_TOKEN, LETTERMINT_TEST_FROM in .env
```

### Releasing

Add changes under `## [Unreleased]` in `CHANGELOG.md` as they land. To release, on an
up-to-date `main` with a clean tree:

```bash
uv run bump-my-version bump patch   # or minor / major; --dry-run -v to preview
git push --follow-tags
```

`bump-my-version` updates `__version__` and `CHANGELOG.md`, commits `chore: release X.Y.Z`
and tags `vX.Y.Z`. The tag starts the Release workflow. It runs the tests, builds, and waits
for approval of the `pypi` environment (Actions → the run → Review deployments), then
publishes with Trusted Publishing.

## License

BSD-3-Clause, the same as django-anymail.
