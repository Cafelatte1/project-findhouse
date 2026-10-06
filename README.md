# project-findhouse — Seoul youth monthly-rent housing notice monitor

Collects public / social / 청년안심주택 rental notices for young people in Seoul, extracts unit prices from attachments,
judges each notice against **your own deposit / monthly-rent limits**, stores daily snapshots in SQLite, and prints a
Markdown alert only when something new, near-miss, closing soon, or broken shows up (quiet otherwise).

It is designed to be run on a schedule by an agent (or cron) that relays the Markdown to chat. Read-only: it never applies,
logs in, or contacts anyone.

## Sources

| Channel | Method |
|---|---|
| SH 인터넷청약 notice board (i-sh.co.kr) | HTML (curl) |
| 청년안심주택 board (soco.seoul.go.kr) | headless Chromium (Playwright) — JS-rendered |
| LH 청약플러스, Seoul region (apply.lh.or.kr) | POST form (curl), with structure-change canary |
| 서울주거포털 (housing.seoul.go.kr) | HTML (curl) |
| 사회주택협회 listings (socialhousing.kr) | HTML (curl) |

Attachments of new / unpriced notices: text PDFs → `pdfplumber` tables, HWP/HWPX → `rhwp-python` tables
(fallback `hwp5html`). **Image-only notices are not OCR'd by default**: the pipeline renders page PNGs and flags the
notice as "price unknown", and the agent reads the images and records the numbers with `units.py`
(optional experimental OCR: `"docs": {"ocr": true}`).

## Quick start

```bash
git clone https://github.com/Cafelatte1/project-findhouse.git
cd project-findhouse
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # or set CHROME_PATH=/path/to/chrome to use an installed Chrome
cp config.example.json config.json   # then set YOUR limits / excluded districts
python selftest.py                   # offline checks, no network
python run_daily.py --no-mark        # live run; --no-mark = don't update "already notified" state
```

System tools: `curl`, poppler-utils (`pdftoppm`, `pdftotext`). Optional: `tesseract-ocr` + `tesseract-ocr-kor` (only if `docs.ocr=true`).

Notes
- **FreeType / rhwp-python**: some Linux builds of `rhwp-python` fail to import with `undefined symbol: FT_Palette_Data_Get`.
  `docs.py` preloads the system `libfreetype.so.6` automatically (install `libfreetype6`; override with `FREETYPE_LIB=/path/libfreetype.so.6`).
  If rhwp still can't load, HWP falls back to `hwp5html` (from `pyhwp`, in requirements) and HWPX to direct XML parsing.
- **Chromium**: resolution order is `CHROME_PATH` → Playwright's bundled Chromium → `google-chrome`/`chromium` on PATH.
  If none is available only the 청년안심주택 source fails; other sources still run and the failure is reported in the alert.
- **Data location**: everything (DB, downloads, `config.json`) lives in the repo folder by default; set `HOUSING_DIR=/some/dir` to keep it elsewhere.

## Configuration (`config.json`, money in 만원 = 10,000 KRW)

| Key | Meaning |
|---|---|
| `max_deposit_manwon`, `max_rent_manwon` | match if **any** unit/deposit-ratio option satisfies both |
| `near_tolerance_deposit_manwon`, `near_tolerance_rent_manwon` | "near miss" band above the limits |
| `exclude_gu` | districts to exclude (empty = none) |
| `due_soon_days` | closing-soon alert window |
| `docs.*` | attachment extraction limits, `ocr` on/off |

CLI overrides: `python run_daily.py --max-deposit 3000 --max-rent 50`.

## Commands

```bash
python run_daily.py [--no-mark] [--summary]           # collect → DB → judge → Markdown alert
python query.py --date YYYY-MM-DD --pretty [--fit match,near]
python query.py --runs | --docs
python units.py meta|unit|list|verify ...             # record human-checked prices from a notice
python docs.py process <source> <item_id> [--force]   # (re)process one notice's attachments
python sources.py sh|soco|lh|seoulportal|socialhousing # test a single source
```

Suggested schedule: twice a day (e.g. 12:30 and 18:30 KST), see `DESIGN.md` §13.

## Data

SQLite `housing.db` (created on first run, git-ignored): `runs`, `items`, `item_meta`, `units`, `listings`
(one snapshot row per notice per `collected_date`), `documents` (attachment cache keyed by sha256 + parser version).
Full design, schema, alert template and limitations: [`DESIGN.md`](DESIGN.md).

## Limitations

- Prices are only taken from the notice (human-checked or parsed tables); nothing is guessed. Unparsed notices show as "price unknown".
- Eligibility (income / assets / subscription account) is not auto-judged; notice criteria are listed for the user to check.
- Site structure changes break parsers; source failures are isolated and reported, LH has an explicit structure-change canary.
- 마이홈포털 is not collected (blocked from many networks); the data.go.kr API is a possible future source.

## License

No license has been chosen yet — the repository owner should pick one (until then, default copyright applies).
