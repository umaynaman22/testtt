# Rental Tracker (offline)

A simple rental tracker that runs **entirely on your own computer, with no internet**. It works for one house or 50+ properties.

- **Add a property**, then **add a tenant** to it. Every field is optional.
- Rent is **billed automatically** each month.
- **Record payments**, one at a time or all at once on the *Collect rent* screen.
- See **balances**, **who's late** and by how many days, and each tenant's full **payment history**.
- Print receipts and statements, and export lists for Excel.

| Document | What's in it |
|---|---|
| [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) | The original full design, and [what the app includes today](docs/BLUEPRINT.md#18-build-status) |
| [`src/rental_tracker/db/migrations/0001_initial.sql`](src/rental_tracker/db/migrations/0001_initial.sql) | The database design (later changes are in the numbered files next to it) |

## Windows app (.exe)

Every push builds a Windows installer and a portable `.exe` on GitHub Actions ([workflow](.github/workflows/windows-build.yml)). To download them:

1. Open the repository's **Actions** tab and choose the latest green **Windows build** run.
2. Under **Artifacts**, download `RentalTracker-windows-<version>` (a zip).
3. Unzip it. Inside are:
   - `RentalTracker-Setup-<version>.exe`: installs for your user account (no admin needed) and adds Start-menu shortcuts, including one that opens the demo data.
   - `RentalTracker-Portable-<version>.exe`: a single file you can run from anywhere, such as a USB stick. It takes a few seconds longer to start.
   - `SHA256SUMS.txt`: checksums, if you want to verify the files.

The files aren't code-signed, so Windows SmartScreen may say *"Windows protected your PC"* the first time. Click **More info → Run anyway**.

The app window uses the Microsoft Edge WebView2 Runtime, which Windows 10 and 11 normally already have. If it's missing, the app opens in your web browser instead and tells you what to install.

Uninstalling never deletes your data in `Documents\RentalTracker`.

To build it yourself on Windows:

```powershell
pip install -r packaging/requirements-build.txt
pip install .
pyinstaller packaging/rental_tracker.spec --noconfirm       # dist\RentalTracker\ and dist\RentalTracker-Portable.exe
iscc /DAppVersion=0.1.0 packaging\installer.iss              # dist\RentalTracker-Setup-0.1.0.exe (needs Inno Setup 6)
```

## Install from source

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
python -m rental_tracker --demo          # try it with a made-up 60-property portfolio (separate folder)
python -m rental_tracker                 # your real data (in Documents/RentalTracker)
python -m rental_tracker --data-dir "D:\Rentals"   # keep the data somewhere else
python -m rental_tracker --browser       # use your web browser instead of a window
```

The app opens in its own window. If pywebview is not installed, it opens in your browser instead. It only listens on `127.0.0.1` (this computer), and each launch uses a new secret link. Close the window, or click **Quit** in the sidebar, to exit; a backup is saved on the way out.

**Getting started:**

1. **Properties → Add property.** Type the address, or leave everything blank and fill it in later.
2. On the property page, click **Add tenant**. A name and the monthly rent are enough. For someone who already lives there, the app bills rent from this month; put anything they owed before in **Already owes**.
3. When rent comes in, open the tenant and use **Record a payment**, or go to **Collect rent** to enter everyone's at once.
4. **Late** shows who is behind and by how many days. The dashboard shows the totals.

Your name or business for receipts, and a default late fee, are under **Settings**.

## Your data

Everything lives in one folder (default `Documents/RentalTracker`):

```
rental.db      the database
backups/       automatic copies (daily, weekly, monthly, and before deletes)
```

A copy of the database is saved automatically every day, when you quit, and before anything is deleted (in `backups/`). To move to a new computer, copy the whole folder while the app is closed.

## Develop

```bash
pip install -e ".[dev]"
pytest                      # tests: money rules, services, every page, security checks
```

The code layout is described in [BLUEPRINT §13](docs/BLUEPRINT.md#13-code-structure). The business rules live in `src/rental_tracker/domain/` as pure functions.
