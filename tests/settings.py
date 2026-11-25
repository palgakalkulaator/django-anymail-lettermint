SECRET_KEY = "tests-only"
INSTALLED_APPS = ["anymail"]
ROOT_URLCONF = "tests.urls"
EMAIL_BACKEND = "anymail_lettermint.EmailBackend"
ANYMAIL = {
    "LETTERMINT_API_TOKEN": "test-token",
    "LETTERMINT_WEBHOOK_SECRET": "whsec_tracking",
    "LETTERMINT_INBOUND_SECRET": "whsec_inbound",
}
DEFAULT_FROM_EMAIL = "from@example.com"
USE_TZ = True
