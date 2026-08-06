"""Human-facing sync pages (require session login; in the salesperson allowlist).

* ``status`` - shows role, server, pending uploads, recent runs and any
  conflicts, plus the Sync/Upload button.
* ``run``    - POST target that performs one push-then-pull and reports back.
"""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from panel.permissions import is_manager

from .client import run_sync
from .models import SyncConflict, SyncLog, SyncOutbox, SyncState


def status(request):
    state = SyncState.get()
    ctx = {
        'active_sidebar': 'sync',
        'role': getattr(settings, 'SYNC_ROLE', 'standalone'),
        'server_url': getattr(settings, 'SYNC_SERVER_URL', ''),
        'node_name': getattr(settings, 'SYNC_NODE_NAME', ''),
        'configured': bool(getattr(settings, 'SYNC_SERVER_URL', '')
                           and getattr(settings, 'SYNC_NODE_TOKEN', '')),
        'pending': SyncOutbox.objects.count(),
        'last_pulled_at': state.last_pulled_at,
        'last_pushed_at': state.last_pushed_at,
        'recent_logs': SyncLog.objects.all()[:10],
        'open_conflicts': SyncConflict.objects.filter(resolved=False).count(),
        'is_manager_user': is_manager(request.user),
        # Managers "Sync Data"; salespeople "Upload".
        'button_label': 'مزامنة البيانات' if is_manager(request.user) else 'رفع بياناتي',
    }
    return render(request, 'sync/status.html', ctx)


@require_POST
def run(request):
    summary = run_sync()
    if summary['ok']:
        messages.success(
            request,
            f"تمت المزامنة: رفع {summary['pushed']}، تنزيل {summary['pulled']}"
            + (f"، تعارضات {summary['conflicts']}" if summary['conflicts'] else ""),
        )
    else:
        messages.error(request, f"فشلت المزامنة: {summary['message']}")
    return redirect('sync:status')
