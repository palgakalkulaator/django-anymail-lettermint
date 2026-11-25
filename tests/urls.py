from django.urls import include, path

urlpatterns = [path("anymail/", include("anymail_lettermint.urls"))]
