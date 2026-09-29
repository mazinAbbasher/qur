"""System update page and the updater's safety checks.

Run with:  python manage.py test panel
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from panel import updater
from panel.permissions import SALESPERSON_GROUP


class UpdaterStateMixin:
    """Point the updater's state files at a temp folder, not the real `.update/`."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / '.update'
        for name, value in {
            'STATE_DIR': state,
            'STATUS_FILE': state / 'status.json',
            'LOG_FILE': state / 'update.log',
            'LOCK_FILE': state / 'lock',
        }.items():
            patcher = mock.patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = Path(tmp.name)


class SystemUpdateViewTests(UpdaterStateMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('rep', password='pw')
        user.groups.add(Group.objects.get_or_create(name=SALESPERSON_GROUP)[0])
        self.client.force_login(user)

    def test_salesperson_can_open_the_page(self):
        response = self.client.get(reverse('panel:system_update'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تحديث النظام')

    def test_button_launches_background_update(self):
        with mock.patch.object(updater, 'launch', return_value=True) as launch:
            response = self.client.post(reverse('panel:system_update_start'))
        self.assertRedirects(response, reverse('panel:system_update'))
        launch.assert_called_once_with('none')  # not under runserver's autoreloader

    @override_settings(SYNC_ROLE='server')
    def test_server_is_updated_from_the_command_line_only(self):
        with mock.patch.object(updater, 'launch') as launch:
            self.client.post(reverse('panel:system_update_start'))
        launch.assert_not_called()

    def test_run_that_died_is_reported_as_failed(self):
        # Status says running, but no process holds the lock (e.g. the laptop
        # was switched off mid-update).
        updater._write_status(state='running', step='x')
        data = self.client.get(reverse('panel:system_update_status')).json()
        self.assertFalse(data['running'])
        self.assertEqual(data['state'], 'failed')

    def test_just_launched_counts_as_running_until_the_grace_period_ends(self):
        updater._write_status(state='starting')
        self.assertTrue(updater.is_running())

        status = updater.read_status()
        status['updated_at'] = time.time() - updater.LAUNCH_GRACE_SECONDS - 1
        updater.STATUS_FILE.write_text(json.dumps(status), encoding='utf-8')
        self.assertFalse(updater.is_running())


class UpdaterLockTests(UpdaterStateMixin, TestCase):
    @unittest.skipIf(os.name == 'nt', 'uses fcntl to hold the lock')
    def test_running_while_another_process_holds_the_lock(self):
        self.assertFalse(updater.is_running())
        updater.STATE_DIR.mkdir(exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, '-c', (
                "import fcntl, sys, time; f = open(sys.argv[1], 'a+b'); "
                "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
            ), str(updater.LOCK_FILE)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            holder.stdout.readline()
            self.assertTrue(updater.is_running())
        finally:
            holder.kill()
            holder.wait()
        self.assertFalse(updater.is_running())  # killed: the OS released it


class LocalChangesTests(UpdaterStateMixin, TestCase):
    """Deleting files (e.g. the sample DBs on a salesperson laptop) must not
    block updates; editing them must."""

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.tmp, check=True, capture_output=True)

    def test_deletions_are_allowed_edits_are_not(self):
        self.git('init', '-q')
        (self.tmp / 'sample.sqlite3').write_text('x')
        (self.tmp / 'views.py').write_text('x')
        self.git('add', '.')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init')
        updater.STATE_DIR.mkdir()

        with mock.patch.object(updater, 'BASE_DIR', self.tmp):
            (self.tmp / 'sample.sqlite3').unlink()
            self.assertEqual(updater._local_changes(), [])

            (self.tmp / 'views.py').write_text('edited')
            self.assertEqual(updater._local_changes(), ['views.py'])
