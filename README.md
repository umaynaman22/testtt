# Rental Tracker (offline)

This repo holds the plan for a desktop rental-management app that runs **entirely on your own computer, with no internet**. It is sized for a portfolio of **50+ properties**.

| Document | What's in it |
|---|---|
| [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) | The full blueprint: goals, architecture, tech stack, features, money rules, screens, reports, backups and security, bulk import, code layout, testing, and a phased roadmap |
| [`docs/schema.sql`](docs/schema.sql) | A ready-to-run SQLite schema: 33 tables, constraints, guard-rail triggers, rent-roll and occupancy views, and seed categories and settings |

**Stack at a glance:** Python 3.12 · Flask + HTMX · SQLite · pywebview desktop window · PyInstaller installer. Everything is bundled locally; nothing loads from the internet.

To try the schema:

```bash
python3 -c "import sqlite3; c = sqlite3.connect('rental.db'); c.executescript(open('docs/schema.sql').read())"
```

Before building, answer the open questions at the end of the blueprint ([§17](docs/BLUEPRINT.md#17-decisions-for-you-to-make-before-building)).
