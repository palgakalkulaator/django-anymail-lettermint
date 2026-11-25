"""
Webhook URLs. Include them under any prefix, for example::

    path("anymail/", include("anymail_lettermint.urls"))

gives /anymail/lettermint/tracking/ and /anymail/lettermint/inbound/.
"""

from django.urls import path

from .webhooks import LettermintInboundWebhookView, LettermintTrackingWebhookView

app_name = "anymail_lettermint"
urlpatterns = [
    path(
        "lettermint/tracking/",
        LettermintTrackingWebhookView.as_view(),
        name="lettermint_tracking_webhook",
    ),
    path(
        "lettermint/inbound/",
        LettermintInboundWebhookView.as_view(),
        name="lettermint_inbound_webhook",
    ),
]
