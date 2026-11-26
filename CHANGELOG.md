# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-26

### Added

- Release tooling: `uv run bump-my-version bump patch|minor|major` updates `__version__` and
  this changelog, commits `chore: release X.Y.Z` and tags `vX.Y.Z`. See README → Releasing.
  (#1)
- `[sdk]` extra: with `pip install "django-anymail-lettermint[sdk]"`, the tracking and inbound
  webhooks verify signatures with the official Lettermint SDK (`lettermint.Webhook`).
  Without it, the built-in check is used as before. Sending is unchanged.
  (#3)

## [0.1.0] - 2026-11-25

### Added

- `anymail_lettermint.EmailBackend`: sends through `POST /v1/send` and `POST /v1/send/batch`
  with Anymail's message API (attachments, inline images, headers, metadata, a single tag,
  open/click tracking, `send_at`, batch send with `merge_metadata`/`merge_headers`).
  Configure it with `MAILERS` (Django 6.1+) or with `EMAIL_BACKEND` + `ANYMAIL`.
- `esp_extra={"idempotency_key": ...}` sends Lettermint's `Idempotency-Key` header.
- `LettermintTrackingWebhookView`: maps Lettermint `message.*` events to Anymail's
  `tracking` signal.
- `LettermintInboundWebhookView`: maps `message.inbound` to Anymail's `inbound` signal.
- Both webhooks verify `X-Lettermint-Signature`. Configure them with
  `LETTERMINT_WEBHOOK_SECRET` and `LETTERMINT_INBOUND_SECRET`.
- `anymail_lettermint.urls` for `include()`.

[Unreleased]: https://github.com/palgakalkulaator/django-anymail-lettermint/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/palgakalkulaator/django-anymail-lettermint/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/palgakalkulaator/django-anymail-lettermint/releases/tag/v0.1.0
