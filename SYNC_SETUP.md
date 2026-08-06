# Multi-Laptop Sync — Setup & Operations Guide

This system now runs across several laptops that share one **central server**:

```
Manager laptop ──┐
Salesperson 1 ───┼── HTTPS + token ──▶  Central Server (source of truth)
Salesperson 2 ───┘                       Postgres, always on
```

Each laptop keeps its own local database and works **offline**. When online it
syncs:

* **Manager** clicks **مزامنة البيانات (Sync Data)** → uploads admin changes and
  downloads the latest of everything.
* **Salesperson** clicks **رفع بياناتي (Upload)** → uploads their new
  sales/clients/payments and downloads read-only reference data (products,
  stock, prices) — **never** purchase costs or other sensitive data.

Nothing is duplicated (records are matched by a global `sync_id`), nothing is
silently overwritten (conflicts are logged for review), and stock is
recomputed authoritatively on the server so it can never go negative.

---

## Part A — Central Server (do this once)

A small Linux VPS (~$5/mo) with a domain or public IP.

### 1. System packages
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip postgresql nginx
```

### 2. Postgres database
```bash
sudo -u postgres psql -c "CREATE DATABASE equatorial;"
sudo -u postgres psql -c "CREATE USER equatorial WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE equatorial TO equatorial;"
```

### 3. Get the code + dependencies
```bash
git clone <your-repo-url> /opt/equatorial && cd /opt/equatorial
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

### 4. Server `.env` (copy from `.env.example`)
```ini
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<run: python -c "from django.core.management.utils import get_random_secret_key as g; print(g())">
DJANGO_ALLOWED_HOSTS=server.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://server.example.com

POSTGRES_DB=equatorial
POSTGRES_USER=equatorial
POSTGRES_PASSWORD=CHANGE_ME
POSTGRES_HOST=localhost

SYNC_ROLE=server
```

### 5. Initialise
```bash
./venv/bin/python manage.py migrate
./venv/bin/python manage.py collectstatic --noinput
./venv/bin/python manage.py createsuperuser         # your admin login
./venv/bin/python manage.py setup_roles             # creates manager/salesperson groups
```

### 6. Register each laptop and copy its token
```bash
./venv/bin/python manage.py create_node "Manager-Laptop" --role manager
./venv/bin/python manage.py create_node "Ahmed-Laptop"   --role salesperson
./venv/bin/python manage.py create_node "Sara-Laptop"    --role salesperson
```
Each prints a `SYNC_NODE_TOKEN=...`. Give each laptop **its own** token.

### 7. Create login accounts (used at each laptop's login screen)
```bash
./venv/bin/python manage.py setup_roles --manager     boss  --password "..."
./venv/bin/python manage.py setup_roles --salesperson ahmed --password "..."
./venv/bin/python manage.py setup_roles --salesperson sara  --password "..."
```

### 8. Run it behind HTTPS
Serve with gunicorn and put nginx (or Caddy for automatic HTTPS) in front,
terminating TLS. Minimal gunicorn service:
```bash
./venv/bin/gunicorn cafe.wsgi --bind 127.0.0.1:8000 --workers 3
```
Point nginx/Caddy at `127.0.0.1:8000` for `server.example.com` with a Let's
Encrypt certificate. HTTPS is required — tokens travel in request headers.

---

## Part B — Manager laptop

1. Install the code + `pip install -r requirements.txt` (as today).
2. `.env`:
   ```ini
   SYNC_ROLE=manager
   SYNC_SERVER_URL=https://server.example.com
   SYNC_NODE_TOKEN=<the Manager-Laptop token from step A6>
   SYNC_NODE_NAME=Manager-Laptop
   ```
3. **First time only — upload the existing data** to the fresh server:
   ```bash
   python manage.py sync_seed     # queues every existing record
   python manage.py sync          # uploads them
   ```
4. Day to day: open the app → **مزامنة البيانات** page → **Sync Data**.

## Part C — Salesperson laptop

1. Install the code + `pip install -r requirements.txt`.
2. `.env`:
   ```ini
   SYNC_ROLE=salesperson
   SYNC_SERVER_URL=https://server.example.com
   SYNC_NODE_TOKEN=<that laptop's salesperson token>
   SYNC_NODE_NAME=Ahmed-Laptop
   ```
3. First run: open **مزامنة البيانات** → **Upload** once to download products,
   stock and prices. Then work normally (offline is fine) and press **Upload**
   whenever online to send new sales and refresh reference data.

> Optional: automate syncing with cron (Linux/Mac) or Task Scheduler (Windows)
> running `python manage.py sync` every few minutes while online.

---

## How conflicts & safety work

* **No duplicates:** every record has a `sync_id`; re-sending updates the same
  row. Pushing twice changes nothing the second time.
* **No clobbering:** if the server sends a record you edited locally but haven't
  uploaded yet, your local copy is kept and the difference is recorded as a
  **conflict** for review — never overwritten.
* **Stock:** the server recomputes each batch's remaining quantity from the
  actual sales/returns/losses. If two reps sell the last unit, the second sale
  is accepted but flagged (`oversell`) and stock is clamped to zero, not left
  negative.
* **Review conflicts:** on the server (or manager laptop) visit
  `/admin/sync/syncconflict/` to see and resolve anything flagged.
* **Sensitive data:** salespeople never receive purchase costs, supplier
  balances, commissions, expenses or finance data — enforced in the views,
  the templates, and again in the sync API.

## Backups
* Server: schedule `pg_dump equatorial` daily.
* A pre-sync backup of the manager's local SQLite is at `db.sqlite3.bak-presync`.

## Roles cheat-sheet
Salespeople now have **broad operational access** — only *financial* data and
pages stay manager-only.

| Capability                                             | Manager | Salesperson |
|--------------------------------------------------------|:------:|:-----------:|
| Sales, invoices, payments, returns                     |   ✓    |      ✓      |
| Create/edit clients, products, areas, lost products    |   ✓    |      ✓      |
| **View** shipments, suppliers, employees, managers     |   ✓    |  ✓ (no cost/commission figures) |
| Create/edit shipments, suppliers, staff                |   ✓    |      ✗      |
| Purchase costs (USD/SDG), profit, net-profit dashboard |   ✓    |      ✗      |
| Commissions, expenses, finance (balances/partners)     |   ✓    |      ✗      |

> **Security note — salesperson laptops:** a salesperson laptop should start
> from an **empty** database and pull from the server, so cost/commission data
> physically never lands on it (the sync strips those fields). Do **not** copy
> the manager's full `db.sqlite3` onto a salesperson laptop — hiding a field in
> the UI is not the same as it being absent from the local database file.
