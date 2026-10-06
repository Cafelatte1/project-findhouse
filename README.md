# project-findhouse — Seoul public-rental housing notice monitor (monthly rent / jeonse · youth / newlyweds)

Collects public / social / 청년안심주택 rental notices in Seoul, extracts unit prices from attachments,
judges each notice against **your own limits** for the lease types you pick (**월세** monthly rent and/or **전세** jeonse)
and the household types you pick (**청년** youth and/or **신혼부부** newlyweds), stores daily snapshots in SQLite, and prints a
Markdown alert only when something new, near-miss, closing soon, or broken shows up (quiet otherwise).
The default config (no new keys) behaves exactly like the original monthly-rent / youth-only monitor.

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
| HUG 든든전세주택 (khug.or.kr, jeonse only) | latest notice PDF (cp949 list page) → Seoul unit count, deposit rule, window, eligibility — notice-level, no per-house prices |
| LH 전세임대 programs (nationwide rolling, support programs only) | POST form + notice detail; open only, and only when the detail's per-municipality supply list has Seoul rows (note `서울 N호 · K개 구`) |

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
| `lease_types` | `["monthly"]` (default), `["jeonse"]` or both |
| `targets` | `["youth"]` (default), `["newlywed"]` or both. Only selected targets are shown; unit rows for other targets (and elderly etc.) are ignored |
| `max_deposit_manwon`, `max_rent_manwon` | monthly: match if **any** unit/deposit-ratio option satisfies both (may also be given as a `monthly` block) |
| `near_tolerance_deposit_manwon`, `near_tolerance_rent_manwon` | "near miss" band above the limits |
| `jeonse.max_deposit_manwon`, `jeonse.near_tolerance_deposit_manwon` | jeonse limit (default 20000 = 2억) and near-miss band (default +5000) |
| `jeonse.jeonse_like_max_rent_manwon`, `jeonse.jeonse_like_min_deposit_manwon` | a monthly option with rent ≤ 10만 **and** deposit ≥ 5000만 (defaults) is "jeonse-like" (🔁) and is also judged in the jeonse block |
| `support_programs` | list 전세임대 programs (tenant finds the house) in a short 📋 section — default on when jeonse is selected |
| `conversion_rate_pct` | optional, display only (never used for judging) |
| `exclude_gu` | districts to exclude (empty = none). Applied per notice and per unit: unit rows whose label/table names an excluded 구 are dropped from judging; a notice whose units are all in excluded districts is dropped |
| `due_soon_days` | closing-soon alert window |
| `docs.*` | attachment extraction limits, `ocr` on/off |

CLI overrides: `python run_daily.py --max-deposit 3000 --max-rent 50 --jeonse-max-deposit 18000 --types monthly,jeonse --targets youth,newlywed`.

## Alert format (chat, default)

One summary line, then one block per selected lease type that has content, each with its own criteria line and
✅ 조건 부합 / ⚠️ 근소 초과 (+ 마감 임박 / 가격 미확인 / 공고 단위 확인) sections, then 📋 지원 프로그램 and ⚠️ 수집 장애.
Example (both types, both targets):

```markdown
**서울 청년·신혼부부 월세·전세 수집** · 2026-10-06
월세 부합 2 · 근소 1 / 전세 부합 1 · 공고 1 / 지원 프로그램 2

**월세**
• 기준: 보증금 ≤3,000만 · 월세 ≤50만

✅ **조건 부합** (2)

• 🏠 **[SH 신혼·신생아 매입임대Ⅰ](url)** · 신혼부부
  - 서도휴빌(2차) · 강동구 · 둔촌동역 · 44.42㎡
  - 보증금 1,953만 / 월세 25.44만 (보증금50%) · 자동추출
  - 무주택세대 자격

**전세**
• 기준: 전세 보증금 ≤2억

✅ **조건 부합** (1)

• 🏠 **[청계로벤하임](url)** · 청년
  - 동묘앞역 · 20㎡
  - 🔁 전환 · 보증금 1억 8,410만 / 월세 3.24만 · 자동추출
  - 접수 10-06 ~ 10-09 · 마감 임박
```

With a single target no target tag is printed, and with a single lease type the block heading is omitted (the original template).
The link text is the complex name (human-recorded name, else a cleaned-up title); notices that bundle many complexes use a short
program name (e.g. `SH 재개발임대 일반모집`) and put the best unit's complex in the first sub-bullet. Each item has at most 3 sub-bullets
(location/area · price · window/eligibility); unknown parts are omitted rather than shown as placeholders.

Targets: a title for one target only (e.g. 신혼·신생아) is shown only when that target is selected; mixed titles (`청년·신혼부부`,
`청년 및 신혼부부`) and untagged notices are shown for either selection, and unit rows for unselected targets are dropped.
(Intentional change: the original monitor dropped mixed titles for youth-only configs.)
`--format cards` prints the legacy card report.

## Commands

```bash
python run_daily.py [--no-mark] [--summary] [--format chat|cards]   # collect → DB → judge → Markdown alert
python query.py --date YYYY-MM-DD --pretty [--fit match,near] [--type monthly|jeonse|support] [--target 신혼부부]
python query.py --runs | --docs
python units.py meta|unit|list|verify ...             # record human-checked prices from a notice
python docs.py process <source> <item_id> [--force]   # (re)process one notice's attachments
python sources.py sh|soco|lh|seoulportal|socialhousing|hug|lh_support # test a single source
```

Suggested schedule: twice a day (e.g. 12:30 and 18:30 KST), see `DESIGN.md` §13.

## Data

SQLite `housing.db` (created on first run, git-ignored): `runs`, `items`, `item_meta`, `units`, `listings`
(one snapshot row per notice per `collected_date`), `documents` (attachment cache keyed by sha256 + parser version).
Full design, schema, alert template and limitations: [`DESIGN.md`](DESIGN.md).

## Limitations

- Prices are only taken from the notice (human-checked or parsed tables); nothing is guessed. Unparsed notices show as "price unknown".
- Eligibility (income / assets / subscription account / no-home household / marriage period) is not auto-judged; a short note is shown and the notice must be checked.
- Jeonse support programs (전세임대) and HUG 든든전세 are notice-level only: no per-house price judgment.
- LH region labels like `서울특별시 외` mean "Seoul and other regions" (not "outside Seoul"); Seoul supply is confirmed from the notice detail's supply list, and notices without Seoul supply are dropped.
- Site structure changes break parsers; source failures are isolated and reported, LH has an explicit structure-change canary.
- 마이홈포털 is not collected (blocked from many networks); the data.go.kr API is a possible future source.

## License

MIT License. See [LICENSE](LICENSE).
