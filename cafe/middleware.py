"""Authentication + role access control.

A single middleware does two jobs:

1. Requires login for every page except an explicit prefix allowlist
   (``settings.LOGIN_EXEMPT_PREFIXES``) — e.g. the login page, static files,
   the PWA service worker and the token-authenticated sync API.

2. Enforces the salesperson access policy: salespeople have broad operational
   access and are blocked only from the manager-only financial pages listed in
   ``panel.permissions`` (net profit, profit reports, commissions and the
   finance app — except currency exchange, which reps may record). A blocked
   page redirects them back to their sales list.
   Managers and superusers are unrestricted.
"""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse

from panel.permissions import is_manager, salesperson_can_access


class AccessControlMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.exempt_prefixes = tuple(getattr(settings, 'LOGIN_EXEMPT_PREFIXES', ()))
        self.login_url = getattr(settings, 'LOGIN_URL', '/accounts/login/')

    def __call__(self, request):
        return self.get_response(request)

    def _is_exempt(self, path):
        return path.startswith(self.exempt_prefixes) or path.startswith(self.login_url)

    def process_view(self, request, view_func, view_args, view_kwargs):
        path = request.path_info

        # Exempt paths (login page, static, PWA, sync API) bypass all checks.
        if self._is_exempt(path):
            return None

        user = getattr(request, 'user', None)

        # 1) Require authentication.
        if user is None or not user.is_authenticated:
            return redirect(f"{self.login_url}?next={request.path}")

        # 2) Managers / superusers: unrestricted.
        if is_manager(user):
            return None

        # 3) Salespeople: full access except the manager-only financial pages.
        match = request.resolver_match
        view_name = match.view_name if match else ''
        if salesperson_can_access(view_name, request.path_info):
            return None

        # Denied — bounce back to the salesperson's home (their sales list).
        try:
            home = reverse('panel:sale_list')
        except Exception:
            home = '/'
        # Avoid a redirect loop if the sales list itself were ever denied.
        if request.path == home:
            return None
        messages.error(request, "ليس لديك صلاحية للوصول إلى هذه الصفحة.")
        return redirect(home)
