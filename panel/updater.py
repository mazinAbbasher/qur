"""Self-update: pull the latest code from GitHub and apply it safely.

Used by the "تحديث النظام" page and ``python manage.py update_system``.

The update runs in its own background process (``launch()``), never inside the
web request: pulling new ``.py`` files makes ``runserver`` restart, which would
kill a request half-way through the update.

The order of the steps is chosen so that a failure at any point leaves the
machine on the previous, working version:

1. Refuse to start if tracked files were edited on this machine, or the branch
   has commits that aren't on GitHub. Only a clean fast-forward is applied.
   (Deleted files are tolerated: salesperson laptops delete the sample DBs.)
2. ``git fetch``. Stop if already up to date.
3. Laptops upload pending sync data first, so nothing queued under the old
   code has to be sent by the new code.
4. If ``requirements.txt`` changed, ``pip install`` the NEW requirements while
   the old code is still checked out.
5. Fast-forward to the new commit, then validate the new code with
   ``manage.py check`` and ``makemigrations --check``.
6. ``collectstatic`` (only when DEBUG is off, i.e. the server).
7. Back up the SQLite database, then ``migrate``.
8. Reload the app.

If steps 5-7 fail, the database backup is restored, the code goes back to the
previous commit and the previous requirements are reinstalled.

Re-running after an interrupted update (laptop switched off mid-way) is safe:
when the code is already current it still applies any unapplied migrations.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connections

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

BASE_DIR = Path(settings.BASE_DIR)
STATE_DIR = BASE_DIR / '.update'           # git-ignored
STATUS_FILE = STATE_DIR / 'status.json'
LOG_FILE = STATE_DIR / 'update.log'
LOCK_FILE = STATE_DIR / 'lock'

# How long after launch() the page shows "starting" while waiting for the
# runner to take the lock, before deciding it failed to start.
LAUNCH_GRACE_SECONDS = 60
GIT_TIMEOUT = 120
PIP_TIMEOUT = 20 * 60
MANAGE_TIMEOUT = 10 * 60
KEEP_DB_BACKUPS = 5

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # Windows only


class UpdateError(Exception):
    """A step failed; the message is shown to the user."""


# --- Status / lock bookkeeping -------------------------------------------

def read_status():
    try:
        return json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write_status(**fields):
    STATE_DIR.mkdir(exist_ok=True)
    status = read_status()
    status.update(fields, updated_at=time.time())
    tmp = STATUS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
    # On Windows the replace fails while the page is reading the file.
    for attempt in range(5):
        try:
            os.replace(tmp, STATUS_FILE)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))


def read_log_tail(max_chars=6000):
    try:
        text = LOG_FILE.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return text[-max_chars:]


# The runner holds an OS file lock for as long as it runs. The OS releases it
# when the process ends, even if it crashes or the laptop is switched off, so
# an interrupted update never leaves the button stuck on "running".

def _lock(fh):
    """Lock ``fh`` exclusively without waiting; OSError if already locked."""
    fh.seek(0)
    if os.name == 'nt':
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh):
    fh.seek(0)
    if os.name == 'nt':
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fh, fcntl.LOCK_UN)


def is_running():
    status = read_status()
    if (status.get('state') == 'starting'
            and time.time() - status.get('updated_at', 0) < LAUNCH_GRACE_SECONDS):
        return True  # launched; the runner hasn't taken the lock yet
    STATE_DIR.mkdir(exist_ok=True)
    with open(LOCK_FILE, 'a+b') as fh:
        try:
            _lock(fh)
        except OSError:
            return True
        _unlock(fh)
    return False


# --- Helpers ---------------------------------------------------------------

def _log(line):
    stamp = datetime.now().strftime('%H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as fh:
        fh.write(f"[{stamp}] {line}\n")
    if sys.stdout.isatty():
        print(line, flush=True)


def _child_env():
    env = dict(os.environ)
    # Never block waiting for a GitHub username/password nobody can type.
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GCM_INTERACTIVE'] = 'never'
    env['PIP_NO_INPUT'] = '1'
    env['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    # Inherited from runserver's autoreloader; must not leak into child commands.
    env.pop('RUN_MAIN', None)
    env.pop('DJANGO_AUTORELOAD_ENV', None)
    return env


def _run(args, timeout, check=True):
    """Run a command in the project folder and log its output.

    Returns stdout, or the CompletedProcess when ``check=False`` (the caller
    then inspects ``returncode`` itself).
    """
    shown = ' '.join('python' if a == sys.executable else str(a) for a in args)
    _log('$ ' + shown)
    try:
        proc = subprocess.run(
            [str(a) for a in args], cwd=BASE_DIR, env=_child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', timeout=timeout, creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise UpdateError(f"انتهت المهلة أثناء: {shown}")
    except OSError as exc:
        raise UpdateError(f"تعذر تشغيل {args[0]}: {exc}")
    output = proc.stdout.rstrip()  # leading spaces matter in `git status`
    if output:
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(output + '\n')
        if sys.stdout.isatty():
            print(output, flush=True)
    if check and proc.returncode != 0:
        raise UpdateError(f"فشل الأمر: {shown}")
    return proc if not check else output


def _git(*args, **kwargs):
    return _run(['git', *args], GIT_TIMEOUT, **kwargs)


def _manage(*args, **kwargs):
    return _run([sys.executable, 'manage.py', *args], MANAGE_TIMEOUT, **kwargs)


def _pip_install(rev):
    """Install requirements.txt exactly as it is in commit ``rev``."""
    content = _git('show', f'{rev}:requirements.txt')
    fd, path = tempfile.mkstemp(prefix='requirements-', suffix='.txt')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(content + '\n')
        _run([sys.executable, '-m', 'pip', 'install', '-r', path], PIP_TIMEOUT)
    finally:
        os.unlink(path)


def _local_changes():
    """Tracked files edited on this machine, ignoring plain deletions."""
    out = _git('status', '--porcelain', '--untracked-files=no')
    changed = []
    for line in out.splitlines():
        if line and not set(line[:2]) <= {' ', 'D'}:
            changed.append(line[3:])
    return changed


def _sqlite_path():
    db = settings.DATABASES['default']
    if db['ENGINE'] != 'django.db.backends.sqlite3':
        return None
    return Path(db['NAME'])


def _backup_db():
    src = _sqlite_path()
    if src is None:
        _log("قاعدة البيانات ليست SQLite: لا توجد نسخة احتياطية تلقائية (استخدم pg_dump).")
        return None
    if not src.exists():
        return None
    dest = src.with_name(f"{src.name}.bak-preupdate-{datetime.now():%Y%m%d-%H%M%S}")
    connections.close_all()
    with closing(sqlite3.connect(src, timeout=60)) as s, closing(sqlite3.connect(dest)) as d:
        s.backup(d)
    _log(f"نسخة احتياطية من قاعدة البيانات: {dest.name}")
    for old in sorted(src.parent.glob(f"{src.name}.bak-preupdate-*"))[:-KEEP_DB_BACKUPS]:
        old.unlink(missing_ok=True)
    return dest


def _restore_db(backup):
    connections.close_all()
    with closing(sqlite3.connect(backup)) as s, \
            closing(sqlite3.connect(_sqlite_path(), timeout=60)) as d:
        s.backup(d)
    _log(f"تمت استعادة قاعدة البيانات من {backup.name}")


def _sync_pending_data():
    """Laptops: upload anything still queued before the code changes."""
    if getattr(settings, 'SYNC_ROLE', '') not in ('manager', 'salesperson'):
        return
    if not (getattr(settings, 'SYNC_SERVER_URL', '') and getattr(settings, 'SYNC_NODE_TOKEN', '')):
        return
    from sync.client import run_sync
    from sync.models import SyncOutbox

    if not SyncOutbox.objects.exists():
        return
    _step("رفع البيانات المعلقة إلى الخادم قبل التحديث")
    summary = run_sync()
    _log(f"sync: {summary['message']}")
    if not summary['ok']:
        raise UpdateError(
            "توجد بيانات لم تُرفع بعد وتعذرت مزامنتها مع الخادم. "
            "لم يتم التحديث؛ حاول مرة أخرى عند توفر الاتصال."
        )


def _step(text):
    _log(f"== {text}")
    _write_status(step=text)


def _reload(mode):
    if mode == 'touch':
        # runserver's autoreloader restarts when a loaded module changes.
        os.utime(BASE_DIR / 'cafe' / 'settings.py')
        return "سيُعاد تشغيل البرنامج تلقائياً خلال ثوانٍ."
    return "أعد تشغيل البرنامج لتطبيق التحديث."


# --- Main entry points ----------------------------------------------------

def current_version():
    """Short description of the checked-out commit, or None if unavailable."""
    try:
        out = subprocess.run(
            ['git', 'log', '-1', '--format=%h%x00%cd%x00%s', '--date=format:%Y-%m-%d %H:%M'],
            cwd=BASE_DIR, capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
        commit, date, subject = out.split('\x00', 2)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return {'commit': commit, 'date': date, 'subject': subject}


def web_update_allowed():
    """The button is for laptops. The server is updated from the command line."""
    return getattr(settings, 'SYNC_ROLE', 'standalone') != 'server'


def launch(reload_mode):
    """Start the update in a detached background process. False if one is running."""
    if is_running():
        return False
    _write_status(state='starting', step='بدء التحديث', started_at=time.time(),
                  finished_at=None, message='')
    LOG_FILE.write_text('', encoding='utf-8')

    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        # Own session: survives runserver restarting and the terminal closing.
        kwargs['start_new_session'] = True
    with open(LOG_FILE, 'ab') as log:
        subprocess.Popen(
            [sys.executable, 'manage.py', 'update_system', f'--reload={reload_mode}'],
            cwd=BASE_DIR, env=_child_env(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True, **kwargs,
        )
    return True


def run_update(reload_mode='none', skip_sync=False):
    """Perform the update in this process. Returns True on success."""
    STATE_DIR.mkdir(exist_ok=True)
    lock = open(LOCK_FILE, 'a+b')
    for _ in range(25):  # the page takes the lock for a moment to check it
        try:
            _lock(lock)
            break
        except OSError:
            time.sleep(0.2)
    else:
        lock.close()
        _log("يوجد تحديث آخر قيد التنفيذ.")
        return False
    _write_status(state='running', step='بدء التحديث', started_at=time.time(),
                  finished_at=None, message='')
    state, message = 'failed', "توقف التحديث قبل اكتماله."
    try:
        state, message = _update(reload_mode, skip_sync)
    except UpdateError as exc:
        message = str(exc)
    except Exception as exc:
        message = f"خطأ غير متوقع: {exc!r}"
    finally:  # never leave the page stuck on "running"
        _log(f"== {message}")
        _write_status(state=state, step='', message=message, finished_at=time.time())
        lock.close()  # releases the lock
    return state != 'failed'


def _update(reload_mode, skip_sync):
    _step("التحقق من حالة الملفات")
    if not shutil.which('git'):
        raise UpdateError("برنامج git غير مثبت على هذا الجهاز.")
    upstream = _run(['git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'],
                    GIT_TIMEOUT, check=False)
    if upstream.returncode != 0:
        raise UpdateError("هذا المجلد غير مرتبط بفرع على GitHub (git upstream).")
    changed = _local_changes()
    if changed:
        raise UpdateError(
            "توجد تعديلات محلية على ملفات البرنامج في هذا الجهاز، لذلك لم يتم التحديث حتى لا تضيع: "
            + '، '.join(changed[:10])
        )

    _step("تنزيل التحديثات من GitHub")
    _git('fetch')
    old = _git('rev-parse', 'HEAD')
    new = _git('rev-parse', '@{u}')

    if old == new:
        _step("البرنامج محدث؛ التحقق من قاعدة البيانات")
        _manage('check')
        pending = _manage('migrate', '--check', check=False)
        if pending.returncode == 0:
            return 'uptodate', "البرنامج محدث بالفعل إلى آخر إصدار."
        backup = _backup_db()
        try:
            _manage('migrate', '--noinput')
        except Exception:
            if backup:
                _restore_db(backup)
            raise
        return 'done', "تم تطبيق تحديثات قاعدة البيانات. " + _reload(reload_mode)

    if _git('merge-base', '--is-ancestor', 'HEAD', '@{u}', check=False).returncode != 0:
        raise UpdateError(
            "نسخة هذا الجهاز تحتوي على تغييرات غير موجودة في GitHub؛ يلزم تحديث يدوي."
        )
    _git('log', '--oneline', f'{old}..{new}')  # what's new, for the log

    if not skip_sync:
        _sync_pending_data()

    reqs_changed = _git('diff', '--quiet', old, new, '--', 'requirements.txt',
                        check=False).returncode != 0
    backup = None
    try:
        if reqs_changed:
            _step("تثبيت المكتبات المطلوبة")
            _pip_install(new)
        _step("تطبيق التحديث")
        _git('merge', '--ff-only', new)
        _step("فحص الإصدار الجديد")
        _manage('check')
        if _manage('makemigrations', '--check', '--dry-run', check=False).returncode != 0:
            raise UpdateError(
                "الإصدار الجديد يحتوي على تغييرات في الجداول بدون ملفات migrations؛ "
                "تم إلغاء التحديث."
            )
        if not settings.DEBUG:
            _step("تجهيز الملفات الثابتة")
            _manage('collectstatic', '--noinput')
        _step("تحديث قاعدة البيانات")
        backup = _backup_db()
        _manage('migrate', '--noinput')
    except Exception as exc:
        rolled_back = _rollback(old, reqs_changed, backup)
        _reload(reload_mode)
        reason = exc if isinstance(exc, UpdateError) else repr(exc)
        if rolled_back:
            raise UpdateError(f"{reason} — تمت إعادة البرنامج إلى الإصدار السابق.")
        raise UpdateError(f"{reason} — وفشل جزء من التراجع التلقائي؛ راجع السجل أدناه.")

    _step("إعادة تشغيل البرنامج")
    return 'done', f"تم التحديث إلى الإصدار {new[:7]}. " + _reload(reload_mode)


def _rollback(old, reqs_changed, backup):
    """Every part is attempted even if an earlier one fails. True if all worked."""
    _step("التراجع إلى الإصدار السابق")
    ok = True
    for action in (
        (lambda: _restore_db(backup)) if backup else None,
        # --keep (not --hard): only touches files that differ between the two
        # commits, so files deleted on purpose (sample DBs) stay deleted.
        lambda: _git('reset', '--keep', old),
        (lambda: _pip_install(old)) if reqs_changed else None,
    ):
        if action is None:
            continue
        try:
            action()
        except Exception as exc:
            ok = False
            _log(f"!! فشل جزء من التراجع: {exc}")
    return ok
