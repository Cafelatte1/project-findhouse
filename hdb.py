"""공용: DB 스키마/연결/판정 로직."""
import sqlite3, json, os, datetime as dt
from pathlib import Path
# 데이터·설정 위치: 환경변수 HOUSING_DIR (기본 = 이 스크립트가 있는 폴더)
BASE = Path(os.environ.get('HOUSING_DIR') or Path(__file__).resolve().parent); DB = BASE/'housing.db'; CFG = BASE/'config.json'
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  collected_date TEXT NOT NULL,           -- KST YYYY-MM-DD
  started_at TEXT, finished_at TEXT,
  config_json TEXT, counts_json TEXT, errors_json TEXT, status TEXT,
  max_deposit_manwon REAL, max_rent_manwon REAL);
CREATE INDEX IF NOT EXISTS ix_runs_date ON runs(collected_date);
CREATE TABLE IF NOT EXISTS items(          -- 영구 레지스트리 (공고별 최초/최종 확인일·알림 상태)
  source TEXT, item_id TEXT,
  first_seen_date TEXT, last_seen_date TEXT, last_hash TEXT,
  notified_fit TEXT,                       -- 마지막으로 알림한 판정@기준 (예 match@<보증금상한>/<월세상한>) - 재알림 방지
  dedup_key TEXT,                          -- 정규화 제목 (재게시 seq 연결)
  PRIMARY KEY(source,item_id));
CREATE TABLE IF NOT EXISTS item_meta(      -- 공고문 확인 후 수동/에이전트 기록
  source TEXT, item_id TEXT, name TEXT, gu TEXT, station TEXT, housing_type TEXT,
  apply_start TEXT, apply_end TEXT, supply_count TEXT, eligibility TEXT, note TEXT,
  verified_at TEXT, PRIMARY KEY(source,item_id));
CREATE TABLE IF NOT EXISTS units(          -- 주택형·보증금비율 옵션별 1행 (만원)
  source TEXT, item_id TEXT, unit_label TEXT, target TEXT,  -- source=수집처(sh/soco..), target: 청년/신혼부부/공통
  area_m2 REAL, deposit REAL, rent REAL, note TEXT,
  origin TEXT DEFAULT 'human',             -- human(사람 확인) | auto(텍스트PDF/HWP 표 자동추출) | ocr(이미지 OCR)
  verified INTEGER DEFAULT 1,              -- 1=사람 확인, 0=미검증(auto/ocr)
  supply INTEGER, doc_id INTEGER,          -- 공급호수(합계 검산 통과 시), 추출 원본 documents.doc_id
  PRIMARY KEY(source,item_id,origin,unit_label));
CREATE TABLE IF NOT EXISTS documents(      -- 공고 첨부파일 처리 이력 (sha256 같으면 재처리 안 함)
  doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT, item_id TEXT, file_url TEXT, file_name TEXT, sha256 TEXT, size INTEGER,
  kind TEXT,                               -- pdf-text | pdf-image | image | hwp | hwpx | zip | other | none
  status TEXT,                             -- ok | no_table | needs_agent | skipped_form | dup_hash | unsupported | no_attachment | error
  method TEXT,                             -- pdfplumber | tesseract-ocr | hwp5html | hwpx-xml | none
  summary TEXT,                            -- JSON: 추출 요약(units수·페이지·크롭…)
  structured_json TEXT,                    -- 구조화 결과(단지/접수/자격/units+페이지참조) — 캐시 재사용
  parser_version TEXT,                     -- 파서 버전(바뀌면 sha256 캐시 무효)
  verified TEXT DEFAULT 'auto',            -- auto | human
  local_path TEXT, processed_at TEXT, error TEXT,
  UNIQUE(source,item_id,file_url));
CREATE INDEX IF NOT EXISTS ix_documents_sha ON documents(sha256);
CREATE TABLE IF NOT EXISTS listings(       -- 수집일자별 스냅샷
  collected_date TEXT, source TEXT, item_id TEXT, run_id INTEGER,
  title TEXT, category TEXT, name TEXT, gu TEXT, station TEXT,
  area_m2 REAL, deposit REAL, rent REAL, rent_per_m2_won REAL,  -- 판정에 쓰인 대표 주택형
  posted TEXT, apply_start TEXT, apply_end TEXT, status TEXT,
  relevant INTEGER, fit TEXT, fit_reason TEXT, fit_basis TEXT,   -- fit_basis: human|auto|ocr|none
  crit_max_deposit REAL, crit_max_rent REAL, crit_near_deposit REAL, crit_near_rent REAL,
  is_new INTEGER, is_changed INTEGER, is_due_soon INTEGER,
  url TEXT, hash TEXT, updated_at TEXT,
  PRIMARY KEY(collected_date,source,item_id));
CREATE INDEX IF NOT EXISTS ix_listings_date ON listings(collected_date, fit);
"""
def connect():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row; c.executescript(SCHEMA)
    if 'dedup_key' not in [r[1] for r in c.execute('PRAGMA table_info(items)')]: c.execute('ALTER TABLE items ADD COLUMN dedup_key TEXT')
    if 'fit_basis' not in [r[1] for r in c.execute('PRAGMA table_info(listings)')]: c.execute('ALTER TABLE listings ADD COLUMN fit_basis TEXT')
    cols = [r[1] for r in c.execute('PRAGMA table_info(documents)')]
    if 'parser_version' not in cols: c.execute('ALTER TABLE documents ADD COLUMN parser_version TEXT')
    if 'structured_json' not in cols: c.execute('ALTER TABLE documents ADD COLUMN structured_json TEXT')
    c.execute('CREATE INDEX IF NOT EXISTS ix_documents_sha_ver ON documents(sha256, parser_version)')
    if 'origin' not in [r[1] for r in c.execute('PRAGMA table_info(units)')]:   # v1 → v2: PK 에 origin 포함하도록 재생성
        c.executescript('''ALTER TABLE units RENAME TO units_v1;
          CREATE TABLE units(source TEXT, item_id TEXT, unit_label TEXT, target TEXT, area_m2 REAL, deposit REAL, rent REAL, note TEXT,
            origin TEXT DEFAULT 'human', verified INTEGER DEFAULT 1, supply INTEGER, doc_id INTEGER, PRIMARY KEY(source,item_id,origin,unit_label));
          INSERT INTO units(source,item_id,unit_label,target,area_m2,deposit,rent,note,origin,verified) SELECT source,item_id,unit_label,target,area_m2,deposit,rent,note,'human',1 FROM units_v1;
          DROP TABLE units_v1;''')
    return c
def load_cfg(over=None):
    p = CFG
    if not p.exists():   # 첫 실행 편의: config.json 이 없으면 예시 설정으로 동작(경고)
        p = BASE / 'config.example.json'
        import sys; print(f'[warn] {CFG.name} 없음 → {p.name} 사용. cp config.example.json config.json 후 값을 조정하세요.', file=sys.stderr)
    cfg = json.loads(p.read_text())
    for k, v in (over or {}).items():
        if v is not None: cfg[k] = v
    return cfg
KST = dt.timezone(dt.timedelta(hours=9))
def kst_today(): return dt.datetime.now(KST).date()
def pick_units(units):
    """판정 근거 선택: 사람 확인값이 있으면 그것만, 없으면 텍스트 자동추출(auto), 그것도 없으면 OCR. returns (basis, units)"""
    for basis in ('human', 'auto', 'ocr'):
        us = [u for u in units if (u.get('origin') or 'human') == basis]
        if us: return basis, us
    return 'none', []
def judge(units, meta, closed, cfg):
    """returns fit, reason, best_unit, basis.
    basis=human → 그대로 판정. basis=auto(텍스트 PDF/HWP 표) → 판정은 그대로 쓰되 사유에 [자동추출] 표기(알림에 '원문 확인 권장').
    basis=ocr → 숫자 신뢰 불가: fit='unverified'(자동추출·미검증), 사유에 잠정 판정을 남겨 루틴 에이전트가 크롭 이미지로 확인."""
    basis, units = pick_units(units)
    f, r, b = _judge(units, meta, closed, cfg)
    if basis == 'auto': r = '[자동추출] ' + r
    if basis == 'ocr' and f not in ('excluded_region',):
        r = f'자동추출(미검증·OCR) 잠정 {f}: {r}'; f = 'closed' if closed else 'unverified'
    return f, r, b, basis
def _judge(units, meta, closed, cfg):
    gu = (meta or {}).get('gu') or ''
    if any(g in gu for g in cfg['exclude_gu']): return 'excluded_region', f'제외 지역 {gu}', None
    us = [u for u in units if (u['target'] or '공통') in ('청년', '공통')]
    md, mr = cfg['max_deposit_manwon'], cfg['max_rent_manwon']
    nd, nr = md + cfg['near_tolerance_deposit_manwon'], mr + cfg['near_tolerance_rent_manwon']
    if units and not us: return ('closed' if closed else 'no'), '청년 대상 주택형 없음(신혼부부형 등)', None
    if not us: return ('closed' if closed else 'unknown'), '주택형·가격 미확인(공고문 확인 필요)', None
    def key(u): return (u['rent'] / u['area_m2']) if u['area_m2'] else 9e9
    m = [u for u in us if u['deposit'] <= md and u['rent'] <= mr]
    if m:
        b = min(m, key=key); return ('closed_match' if closed else 'match'), f"{b['unit_label']} {b['deposit']:g}/{b['rent']:g}만원", b
    n = [u for u in us if u['deposit'] <= nd and u['rent'] <= nr]
    if n:
        b = min(n, key=key); return ('closed' if closed else 'near'), f"근소초과 {b['unit_label']} {b['deposit']:g}/{b['rent']:g}만원", b
    b = min(us, key=lambda u: (u['deposit'], u['rent']))
    return ('closed' if closed else 'no'), f"조건 밖 (최저 보증금 {b['deposit']:g}만/{b['rent']:g}만)", b
