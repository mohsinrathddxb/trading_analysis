# Telegram Strike Monitor — Visual Studio Windows App

This solution adds a WPF Windows dashboard around the existing Python Telegram/OCR engine.
The proven Python logic remains responsible for Telegram, Tesseract OCR, snapshot time extraction,
strike comparison, SQLite storage, and report generation. The C# WPF application starts and stops
the engine, shows live logs, displays the latest report, and opens the project data folders.

## Solution structure

```text
TelegramStrikeMonitor.sln
src/TelegramStrikeMonitor.App/     WPF desktop application
python/live_strike_monitor.py      Telegram/OCR analysis engine
python/monitor_config.json         OCR and report settings
python/downloaded_images/YYYY-MM-DD/  Telegram images grouped by market day
python/.env.example                Telegram credential template
setup-python.cmd                   Creates venv and installs Python packages
```

## Visual Studio requirements

Open Visual Studio Installer and ensure these workloads are installed:

- .NET desktop development
- Python development

The application targets `net8.0-windows`.

## First setup

1. Run `setup-python.cmd`.
2. Open `python/.env` and add your Telegram API ID and API hash.
3. Confirm Tesseract is installed and `tesseract --version` works.
4. Open `TelegramStrikeMonitor.sln` in Visual Studio.
5. Set `TelegramStrikeMonitor.App` as the startup project.
6. Press `F5`.
7. The app normally detects `python/.venv/Scripts/python.exe`. Use Browse only when your interpreter is elsewhere.
8. The app automatically starts **Today catch-up + live monitoring** when its window opens.

The desktop app now provides one-click operation: opening it automatically
starts today's catch-up and live monitoring. The manual Start and Stop controls
remain available for troubleshooting.

Three on-demand buttons create fresh easy reports from the latest stored
NIFTY and BANKNIFTY images: **Last 15 min**, **Last 30 min**, and **Last 45
min**. Regeneration runs as a separate short-lived process, so live Telegram
monitoring continues. Each output contains both instruments and is written as
`regenerated_<minutes>min_combined_report.txt` in the current market-day
report folder, then loaded into the dashboard.

The dashboard also checks the current market-day reports for both NIFTY and
BANKNIFTY. During market hours it shows the latest time received for each
instrument. If one feed is missing, falls behind the other, becomes stale, or a
corresponding screenshot cannot be identified by OCR, the dashboard turns the
market-data health card red and shows a rate-limited warning message box with
the affected instrument and the last successfully received times.

## Updating and restarting

Select **Update and restart** in the desktop app to run the external updater.
It safely stops monitoring, preserves `.env`, Telegram sessions, databases,
images, reports, and local settings, checks the GitHub `origin/main` source,
installs Python requirements, runs the regression tests, builds into a staging
folder, deploys the verified build, and reopens the app. Monitoring starts
automatically after relaunch. If deployment fails, the updater restores and
restarts the previous application build.

When tracked local source edits exist, the updater does not pull or overwrite
them. It builds and tests the local working tree instead. Commit or otherwise
resolve local edits before using the button to fetch later GitHub changes.

## 45-minute direction analysis

The Python engine reconstructs the complete **Intraday Trend** table and
calculates 15-minute (two-row), 30-minute (three-row), and 45-minute (four-row)
views. Arithmetic averages of Call, Put, Diff, PCR, Price, and VWAP form each
directional baseline. The final 45-minute forecast requires agreement with at
least one shorter timeframe. The latest row remains a separate confirmation so
one noisy row cannot reverse the whole window. The engine keeps these concepts
separate in every report:

- **45-minute average condition** — bullish, bearish, or balanced from the
  normalized four-row average Call/Put Diff.
- **Latest row condition** — the newest row, used to confirm a transition.
- **45-minute flow momentum** — whether PCR, Diff, and Call/Put totals are
  improving or weakening over the window.
- **Next 45-minute forecast** — UP, DOWN, FLAT, or not issued.

Reports also rank the largest **total-OI changes** independently over the exact
45-minute, 30-minute, and 15-minute endpoints. This is positioning activity
and is deliberately not labeled as traded volume. Primary support is
estimated from Put OI concentration, primary resistance from Call OI
concentration, developing levels from positive Change in OI, and weakening
levels from negative Change in OI. All levels include an OCR/data confidence
and remain estimates rather than guaranteed turning points.

The composite also checks Price versus VWAP, Option Signal, VWAP Signal, and
the strike-level option-chain changes. An UP forecast is blocked while the
Call/Put Diff is still negative; a DOWN forecast is blocked while it is
positive. This prevents a weakening bearish condition from being mislabeled
as a confirmed bullish reversal.

Option-chain fields are located from their headers rather than assumed column
positions. Total OI values displayed in lakhs are converted to absolute units.
Signed Intraday Call/Put flows are supported, and a missing Diff OCR cell is
recomputed as `Put - Call`.

## Validation output

Snapshot analysis files are written under the market-date directory
`python/reports/YYYY-MM-DD/`, which is created automatically:

- `snapshot_<id>_combined_report.txt` — the easy-reading report sent to
  Telegram and shown as latest in the app. It shows the current price and
  VWAP, simple 15/30/45-minute views, exact support and resistance strikes,
  one option-selling decision, short reasons, and data quality. Prices and
  strikes are never abbreviated as K/L/Cr.
- `snapshot_<id>_15min_report.txt`, `snapshot_<id>_30min_report.txt`, and
  `snapshot_<id>_45min_report.txt` — standalone detailed reports containing
  the rows used, Call/Put/Diff/PCR averages, momentum, forecast, component
  scores, an up-to-15-strike Call/Put total-OI comparison for that report's
  exact timeframe,
  support/resistance, confirmation, and OCR quality. Strike comparisons show
  total activity, net Put-minus-Call pressure, dominance, interpretation, and
  total-OI deltas when available. Each fixed-width TXT table includes the
  current and comparison times, `|` separators, `+---+` borders, and `n/a`
  when the required endpoint is unavailable.
- Each combined and individual report includes a historical option-premium
  probability table by strike and option type. A win is measured from later
  observed LTP after the configured round-trip cost. Results include sample
  size, a 95% Wilson interval, average winning/losing return, expected value,
  and an OCR/data-quality filter. Probability remains `n/a` until at least 100
  independent completed timestamps across 20 trading days exist. Sell
  candidates require the
  direction, probability, expected-value, and quality gates to agree; option
  selling is labeled as requiring a defined-risk hedge.
- Option-selling candidates now use a strict all-gates rule. The historical
  sample must contain at least 100 independent timestamps across 20 trading
  days, the estimated win rate must be at least 60%, its 95% lower confidence
  bound must be at least 52%, and net expected value must be positive. The
  historical tail loss and current credit-spread maximum loss must also remain
  within configured limits.
- VWAP and 9/21 EMA direction, selected-side OI buildup, minimum premium/OCR
  quality, verified traded volume, and an available farther-OTM hedge must all
  confirm. Every pass/fail result and its evidence is printed in the report.
  Because the current screenshots do not expose verified traded volume, that
  gate remains `N/A` and safely blocks a sell signal until a real volume source
  is added. OI is never relabeled as volume.
- Option exits must match their exact 15/30/45-minute target within 90 seconds.
  Missing targets remain unevaluated instead of borrowing the next 15-minute
  snapshot. Correlated strikes at one market time are collapsed into one
  median outcome before probability and confidence calculations.
- Combined reports longer than one Telegram message are sent in numbered
  parts so the strike comparison and conclusion are not truncated.
- `snapshot_<id>_strike_diff.csv` — strike-by-strike comparison data for the
  snapshot when an earlier comparable snapshot exists.

The cumulative validation files remain at the root of `python/reports`:

- `prediction_backtest.csv` — every stored prediction and its later observed
  45-minute result.
- `prediction_backtest_summary.txt` — walk-forward accuracy and coverage by
  instrument. Predictions are evaluated only after their target time, so
  future rows are never used to create the prediction.
- `option_probability_backtest.csv` records each strike-level Call/Put entry
  and its later 15/30/45-minute exit premium. Only outcomes observed after
  their target time enter the probability calculation.

Run the regression tests with:

```cmd
cd python
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Telegram images are stored under
`python/downloaded_images/YYYY-MM-DD/`. The date is calculated in the
configured market timezone, and the folder is created automatically when the
first image for that day arrives. Legacy flat files are organized into their
daily folders on the next engine start.

## Important files excluded from Git

The `.gitignore` intentionally excludes:

- `python/.env`
- Telegram `.session` files
- downloaded images
- reports and OCR debug files
- SQLite runtime database
- Python virtual environment

Do not commit Telegram API credentials or session files.

## Recommended Git workflow

```cmd
git init
git add .
git commit -m "Initial Visual Studio Windows monitor"
```

Create a new branch before a major OCR change:

```cmd
git switch -c feature/improve-intraday-time-ocr
```

## Build a Windows executable

In Visual Studio, use **Build > Publish** for the WPF project. A normal framework-dependent publish still needs:

- the included `python` engine folder;
- a configured Python environment;
- Tesseract OCR;
- Telegram `.env` and session authorization.

For deployment to another Windows x64 computer, use the safe offline installer
described below; it bundles the required application and OCR runtimes.

## Safe installer for another Windows computer

The Windows x64 installer is built from `installer/TelegramStrikeMonitor.iss`.
It contains the self-contained WPF application, a portable Python runtime,
Python OCR dependencies, and the English Tesseract runtime. It intentionally
does not contain `python/.env`, Telegram session files, databases, downloaded
images, or reports.

The ready-to-install Windows x64 setup for version 1.2.2 is stored at:

`installer/releases/TelegramStrikeMonitor-Setup-1.2.2-win-x64.exe`

Git LFS is used for the installer binary. The accompanying SHA-256 checksum
and installation notes are in the same folder.

On first launch, the application asks for the Telegram API ID, API hash,
source channel, and report destination. These values are written only to the
installed computer. It then opens a one-time console for Telegram phone/code
authorization. After authorization succeeds, catch-up and live monitoring
start automatically.
