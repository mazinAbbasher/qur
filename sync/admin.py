from django.contrib import admin

from .models import (
    Node, SyncConflict, SyncLog, SyncOutbox, SyncState, SyncTombstone,
)


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'is_active', 'last_seen', 'created_at')
    list_filter = ('role', 'is_active')
    search_fields = ('name',)
    readonly_fields = ('token', 'created_at', 'last_seen')


@admin.register(SyncConflict)
class SyncConflictAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'reason', 'model_label', 'sync_id', 'node_name', 'resolved')
    list_filter = ('reason', 'resolved', 'model_label')
    search_fields = ('sync_id', 'detail', 'node_name')
    readonly_fields = ('created_at', 'model_label', 'sync_id', 'reason', 'detail',
                       'incoming', 'existing', 'node_name')
    actions = ['mark_resolved']

    @admin.action(description="Mark selected conflicts as resolved")
    def mark_resolved(self, request, queryset):
        queryset.update(resolved=True)


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ('started_at', 'direction', 'node_name', 'pushed', 'pulled',
                    'conflicts', 'ok')
    list_filter = ('direction', 'ok')
    readonly_fields = [f.name for f in SyncLog._meta.fields]


@admin.register(SyncOutbox)
class SyncOutboxAdmin(admin.ModelAdmin):
    list_display = ('model_label', 'sync_id', 'deleted', 'enqueued_at')
    list_filter = ('model_label', 'deleted')


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ('key', 'last_pull_cursor', 'last_pulled_at', 'last_pushed_at')


@admin.register(SyncTombstone)
class SyncTombstoneAdmin(admin.ModelAdmin):
    list_display = ('model_label', 'sync_id', 'sync_updated_at')
    list_filter = ('model_label',)
