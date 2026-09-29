# Admin Guide — Setting Up a New Laptop

This is the step-by-step checklist for the **admin**: the person who installs the
system on laptops, hands out access, and keeps everything syncing.

For how sync works (conflicts, what salespeople can see) and the one-time
**central server** install, see [SYNC_SETUP.md](SYNC_SETUP.md). This guide
assumes the server is already running at `https://server.example.com`.

```
Manager laptop ──┐
Salesperson 1 ───┼── HTTPS + token ──▶  Central Server
Salesperson 2 ───┘
```

Every laptop has its **own local database** and its **own login accounts**.
Only business data (clients, sales, products…) syncs through the server.
Login accounts do **not** sync.

---

## Before you start: plan the laptop numbers

Every machine needs a unique digit, `SYNC_NODE_NUMBER`. It becomes the first
digit of every invoice number created on that machine, so two machines can never
create the same invoice number. **Two laptops with the same digit will eventually
break sync.**

Keep a record like this (never write the tokens in it):

| Machine          | Role        | `SYNC_NODE_NAME` | `SYNC_NODE_NUMBER` |
|------------------|-------------|------------------|:------------------:|
| Central server   | server      | —                | 0                  |
| Manager laptop   | manager     | Manager-Laptop   | 1                  |
| Ahmed's laptop   | salesperson | Ahmed-Laptop     | 2                  |
| Sara's laptop    | salesperson | Sara-Laptop      | 3                  |

Digits 1–9 are available, so the system supports up to 9 laptops. When you
replace a laptop, give the new one an unused digit if one is free.

---

## Step 1 — Register the laptop on the server

On the **server**, in the project folder:

```bash
./venv/bin/python manage.py create_node "Ahmed-Laptop" --role salesperson
# or, for the manager's laptop:
./venv/bin/python manage.py create_node "Manager-Laptop" --role manager
```

It prints a line like `SYNC_NODE_TOKEN=abc123…`. **Copy it now.** You'll need it
in step 4. Every laptop gets its own token. Never share one between laptops.

---

## Step 2 — Install the requirements on the laptop

**Python 3.12 or newer is required.** The project uses Django 6.0, which does not
install on older Python. On Python 3.10 (the Ubuntu 22.04 default) you'll get:
`No matching distribution found for Django==6.0.8`.

### Ubuntu 24.04 (Python 3.12 included)
```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
```

### Ubuntu 22.04 (needs a newer Python)
```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y git python3.12 python3.12-venv \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0
```
Then use `python3.12` instead of `python3` in step 3.

### Windows
1. Install **Python 3.12+** from python.org. Tick **"Add python.exe to PATH"**.
2. Install **Git for Windows**.
3. For PDF export (WeasyPrint), install **MSYS2**. In the MSYS2 terminal, run
   `pacman -S mingw-w64-x86_64-pango`. Then set a Windows environment variable
   `WEASYPRINT_DLL_DIRECTORIES=C:\msys64\mingw64\bin`.

> The `libpango…` packages / MSYS2 are only needed for PDF invoices and reports.
> Without them the app runs, but PDF buttons fail with an error about
> `libpango` or `gobject`.

---

## Step 3 — Get the code

You need access to the GitHub repo.

**Linux:**
```bash
git clone https://github.com/mazinAbbasher/qur.git ~/qur
cd ~/qur
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/mazinAbbasher/qur.git $HOME\qur
cd $HOME\qur
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

From now on, every `python manage.py …` command assumes you're in the project
folder with the venv **activated** (`source venv/bin/activate` on Linux,
`.\venv\Scripts\Activate.ps1` on Windows).

**On a salesperson laptop, delete the sample databases** that come with the repo.
They contain cost data a salesperson must not have:
```bash
rm 1db.sqlite3 db2.sqlite3
```
Never copy the manager's `db.sqlite3` onto a salesperson laptop.

---

## Step 4 — Create the `.env` file

Create a file named `.env` in the project folder, next to `manage.py`.

**Manager laptop:**
```ini
SYNC_ROLE=manager
SYNC_SERVER_URL=https://server.example.com
SYNC_NODE_TOKEN=<token from step 1>
SYNC_NODE_NAME=Manager-Laptop
SYNC_NODE_NUMBER=1
```

**Salesperson laptop:**
```ini
SYNC_ROLE=salesperson
SYNC_SERVER_URL=https://server.example.com
SYNC_NODE_TOKEN=<token from step 1>
SYNC_NODE_NAME=Ahmed-Laptop
SYNC_NODE_NUMBER=2
```

Check that the settings were picked up:
```bash
python manage.py shell -c "from django.conf import settings as s; print(s.SYNC_ROLE, s.SYNC_NODE_NAME, s.SYNC_NODE_NUMBER, s.SYNC_SERVER_URL)"
```
You should see your values, e.g. `salesperson Ahmed-Laptop 2 https://server.example.com`.
If it prints `standalone this-laptop 0`, the `.env` was ignored. Make sure the file
sits next to `manage.py` and that `pip install -r requirements.txt` finished
without errors (it installs `python-dotenv`, which reads `.env`).

---

## Step 5 — Create the local database and logins

```bash
python manage.py migrate
python manage.py setup_roles          # creates the manager / salesperson groups
```

Now create the login for whoever uses **this** laptop. Accounts made on the
server or on another laptop don't exist here.

**Manager laptop.** Create an admin account. It is needed for the `/admin/`
pages (conflict review, laptop management, client CSV import):
```bash
python manage.py createsuperuser
```
A user made with `setup_roles --manager` can use the app, but on `/admin/` it
sees "You don't have permission to view or edit anything". Use `createsuperuser`
for the admin.

**Salesperson laptop:**
```bash
python manage.py setup_roles --salesperson ahmed --password "choose-a-password"
```

---

## Step 6 — First sync

Start the app (see step 7), log in, and open the sync page:
**مزامنة البيانات** in the sidebar, or `http://127.0.0.1:8000/sync/`.

* **Manager laptop:** click **مزامنة البيانات (Sync Data)**. It downloads
  everything from the server.
* **Salesperson laptop:** click **رفع بياناتي (Upload)** once. It downloads
  products, stock, prices and clients. Costs and finance data are never sent.

The page shows the role, server URL, last sync time and a green success message.
If it fails, see [Troubleshooting](#troubleshooting).

> **`sync_seed` is not part of setting up a new laptop.** It is run **once,
> ever**: on the laptop that held the original data, when the server was first
> created ([SYNC_SETUP.md](SYNC_SETUP.md) Part B step 3). A new laptop just pulls.

---

## Step 7 — Running the app every day

```bash
cd ~/qur
source venv/bin/activate
python manage.py runserver 127.0.0.1:8000
```
Then open **http://127.0.0.1:8000** in the browser. Keep the `127.0.0.1`.
Laptops run in debug mode, so the app must not be reachable from the network.

To save the user typing, put those lines in a `start.sh` (Linux) or `start.bat`
(Windows) desktop shortcut.

**Optional: sync automatically.** On Linux, `crontab -e` and add:
```
*/10 * * * * cd /home/USER/qur && venv/bin/python manage.py sync >> sync.log 2>&1
```
On Windows, use Task Scheduler to run `venv\Scripts\python.exe manage.py sync` in
the project folder every 10 minutes. Failed runs just retry next time. Nothing is
lost while offline.

---

## Admin tasks

### Check which laptops are syncing
Server → `/admin/sync/node/`. The **Last seen** column shows each laptop's last
successful contact.

### Review conflicts
Conflicts are recorded in two places:
* **Server** → `/admin/sync/syncconflict/`: overselling (two reps sold the last
  unit) and stale overwrites.
* **Each laptop** → the sync page shows an "open conflicts" count. On the manager
  laptop, open `/admin/sync/syncconflict/` to see them.

Fix the data if needed, then select the rows and use **Mark selected conflicts
as resolved**.

### A laptop is lost, stolen, or a salesperson leaves
Server → `/admin/sync/node/` → open that laptop → untick **Is active** → Save.
Its token stops working immediately. If you get the laptop back, delete its
`db.sqlite3`.

### Issue a new token (token leaked, or laptop reinstalled)
```bash
./venv/bin/python manage.py create_node "Ahmed-Laptop" --role salesperson --rotate
```
Put the new token in that laptop's `.env`. The old token stops working.

### Reset a user's password on a laptop
On **that laptop**:
```bash
python manage.py setup_roles --salesperson ahmed --password "new-password"
python manage.py changepassword <admin-username>    # for a superuser
```

### Import many clients from a spreadsheet
On the manager laptop: `/admin/panel/client/` → **استيراد من CSV** (Import CSV). Columns: `name`
(required), `phone`, `address`, `area`. Save from Excel as *CSV UTF-8*. Then
sync to send the new clients to the server.

### Update the software
**Update the server first**, then each laptop.

On each laptop, **sync first** so nothing is waiting to upload. Then:
```bash
git pull
pip install -r requirements.txt
python manage.py migrate
```
On the server, also run `collectstatic --noinput` and restart gunicorn.

### Backups
* Server: daily `pg_dump equatorial > backup-$(date +%F).sql`. This is the main
  copy of all data.
* Manager laptop: copy `db.sqlite3` somewhere safe from time to time.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `pip install` fails: *No matching distribution found for Django==6.0.8* | Python is older than 3.12. See step 2. |
| `ModuleNotFoundError: No module named 'crispy_forms'` | The venv isn't activated, or `pip install -r requirements.txt` didn't finish. |
| Sync page says *not configured*, or the role shows `standalone` | `.env` isn't being read. Run the check in step 4. |
| Sync fails with `401 … unauthorized` | Wrong token, or the laptop was deactivated on the server. Check `/admin/sync/node/`, or issue a new token with `--rotate`. |
| Sync fails with `ConnectionError` / timeout | No internet, or the server is down. Work continues offline. Sync again later. |
| Sync fails with a `UNIQUE constraint` / `IntegrityError` on an invoice number | Two machines share the same `SYNC_NODE_NUMBER`. Give each a unique digit. |
| A user can't log in on a laptop | The account doesn't exist **on that laptop**. Create it there (step 5). |
| PDF buttons give an error about `libpango` / `gobject` | WeasyPrint system libraries are missing. See step 2. |
| Manager sees "You don't have permission…" in `/admin/` | Log in with a `createsuperuser` account instead (step 5). |
