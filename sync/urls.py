"""URL routes for the sync app.

The token-authenticated machine-to-machine API lives under ``/sync/api/`` and is
exempt from session login (see ``settings.LOGIN_EXEMPT_PREFIXES``). The
human-facing status / trigger pages live under ``/sync/`` and require login.
"""

from django.urls import path

from . import api, views

app_name = 'sync'

urlpatterns = [
    path('', views.status, name='status'),
    path('run/', views.run, name='run'),
    path('api/pull/', api.api_pull, name='api_pull'),
    path('api/push/', api.api_push, name='api_push'),
    path('api/currency-exchange/', api.api_currency_exchange, name='api_currency_exchange'),
]
