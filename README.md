# Rental Tracker (offline)

A rental property manager that runs **entirely on your own computer, with no internet**. It is built for portfolios of **50+ properties**.

It covers properties, units, tenants and leases, and bills rent automatically with proration and late fees. You record payments on a fast "Rent Day" grid and print receipts. It also tracks security deposits and expenses with receipts, and produces 12 reports, including rent roll, aging, P&L and Schedule E. Backups happen automatically, and you can bulk-import your properties from spreadsheets.

| Document | What's in it |
|---|---|
| [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) | The design: architecture, money rules, features, screens, reports, backups, security, roadmap, and [build status](docs/BLUEPRINT.md#18-build-status) |
| [`src/rental_tracker/db/migrations/0001_initial.sql`](src/rental_tracker/db/migrations/0001_initial.sql) | The database: 33 tables, constraints, guard-rail triggers, rent-roll and occupancy views, and seed categories and settings |

## Install

You need **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/); on Windows, tick "Add Python to PATH").

```bash
git clone https://github.com/umaynaman22/testtt.git
cd testtt
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -e ".[desktop]"     # Flask, plus pywebview for a native window
```

Installing is the only step that needs the internet. After that, the app never goes online.

## Run

```bash
python -m rental_tracker --demo          # try it with a made-up 60-property portfolio
python -m rental_tracker                 # your real data (in Documents/RentalTracker)
python -m rental_tracker --data-dir "D:\Rentals"   # keep the data somewhere else
python -m rental_tracker --browser       # use your web browser instead of a window
```

The app opens in its own window. If pywebview is not installed, it opens in your browser instead. It only listens on `127.0.0.1` (this computer), and each launch uses a new secret link.

**First steps with real data:**

1. **Owners** → add yourself or your LLC.
2. **Import** → download the CSV templates and fill them in. Run **Check files** (this saves nothing), fix any errors it lists, then click **Import now**.
3. **Backups** → set an external drive folder.
4. Each month, open **Rent Day**, type each payment and press Enter. Then review **Late fees**.

## Your data

Everything lives in one folder (default `Documents/RentalTracker`):

```
rental.db      the database      documents/   receipts, leases, photos
backups/       daily, weekly, monthly and safety copies     imports/   archived CSV imports
```

To move to a new computer, copy the folder and start the app with `--data-dir` pointing at it. Never copy `rental.db` while the app is running; use **Backups → Back up now** instead.

## Develop

```bash
pip install -e ".[dev]"
pytest                      # 76 tests: money rules, services, every page, security checks
```

The code layout is described in [BLUEPRINT §13](docs/BLUEPRINT.md#13-code-structure). The business rules live in `src/rental_tracker/domain/` as pure functions.
