"""공용: DB 스키마/연결/판정 로직."""
import sqlite3, json, os, re, datetime as dt
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
  notified_fit TEXT,                       -- 월세: 마지막으로 알림한 판정@기준 (예 match@<보증금상한>/<월세상한>) - 재알림 방지
  notified_jeonse TEXT,                    -- 전세: 판정@전세상한
  notified_support TEXT,                   -- 지원 프로그램: 'seen' (신규 1회 알림)
  dedup_key TEXT,                          -- 정규화 제목 (재게시 seq 연결)
  PRIMARY KEY(source,item_id));
CREATE TABLE IF NOT EXISTS item_meta(      -- 공고문 확인 후 수동/에이전트 기록
  source TEXT, item_id TEXT, name TEXT, gu TEXT, station TEXT, housing_type TEXT,
  apply_start TEXT, apply_end TEXT, supply_count TEXT, eligibility TEXT, note TEXT,
  verified_at TEXT, PRIMARY KEY(source,item_id));
CREATE TABLE IF NOT EXISTS units(          -- 주택형·보증금비율 옵션별 1행 (만원)
  source TEXT, item_id TEXT, unit_label TEXT, target TEXT,  -- source=수집처(sh/soco..), target: 청년/신혼부부/공통/기타(고령자 등, 판정 제외)
  area_m2 REAL, deposit REAL, rent REAL, note TEXT,
  origin TEXT DEFAULT 'human',             -- human(사람 확인) | auto(텍스트PDF/HWP 표 자동추출) | ocr(이미지 OCR)
  verified INTEGER DEFAULT 1,              -- 1=사람 확인, 0=미검증(auto/ocr)
  supply INTEGER, doc_id INTEGER,          -- 공급호수(합계 검산 통과 시), 추출 원본 documents.doc_id
  lease_type TEXT,                         -- monthly | jeonse (월세 0 = 전세 표). NULL = monthly(구 데이터)
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
  lease_type TEXT, targets TEXT,           -- 공고 분류: monthly|jeonse|support · 제목/카테고리상 대상(청년,신혼부부; 빈값=일반)
  fit_monthly TEXT, reason_monthly TEXT, fit_jeonse TEXT, reason_jeonse TEXT,   -- 유형별 판정 (NULL=해당 없음)
  best_json TEXT, elig_note TEXT, extra_note TEXT,                               -- 유형별 대표 주택형 JSON · 자격 한 줄 · 소스 메모
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
    _add_cols(c, 'items', ['notified_jeonse TEXT', 'notified_support TEXT'])           # v3: 전세·신혼부부
    _add_cols(c, 'listings', ['lease_type TEXT', 'targets TEXT', 'fit_monthly TEXT', 'reason_monthly TEXT', 'fit_jeonse TEXT',
                              'reason_jeonse TEXT', 'best_json TEXT', 'elig_note TEXT', 'extra_note TEXT'])
    if 'origin' not in [r[1] for r in c.execute('PRAGMA table_info(units)')]:   # v1 → v2: PK 에 origin 포함하도록 재생성
        c.executescript('''ALTER TABLE units RENAME TO units_v1;
          CREATE TABLE units(source TEXT, item_id TEXT, unit_label TEXT, target TEXT, area_m2 REAL, deposit REAL, rent REAL, note TEXT,
            origin TEXT DEFAULT 'human', verified INTEGER DEFAULT 1, supply INTEGER, doc_id INTEGER, PRIMARY KEY(source,item_id,origin,unit_label));
          INSERT INTO units(source,item_id,unit_label,target,area_m2,deposit,rent,note,origin,verified) SELECT source,item_id,unit_label,target,area_m2,deposit,rent,note,'human',1 FROM units_v1;
          DROP TABLE units_v1;''')
    _add_cols(c, 'units', ['lease_type TEXT'])
    return c
def _add_cols(c, table, defs):
    have = [r[1] for r in c.execute(f'PRAGMA table_info({table})')]
    for d in defs:
        if d.split()[0] not in have: c.execute(f'ALTER TABLE {table} ADD COLUMN {d}')
def load_cfg(over=None):
    p = CFG
    if not p.exists():   # 첫 실행 편의: config.json 이 없으면 예시 설정으로 동작(경고)
        p = BASE / 'config.example.json'
        import sys; print(f'[warn] {CFG.name} 없음 → {p.name} 사용. cp config.example.json config.json 후 값을 조정하세요.', file=sys.stderr)
    cfg = json.loads(p.read_text())
    for k, v in (over or {}).items():
        if v is not None: cfg[k] = v
    return normalize_cfg(cfg, over)

MONTHLY_KEYS = ('max_deposit_manwon', 'max_rent_manwon', 'near_tolerance_deposit_manwon', 'near_tolerance_rent_manwon')
TARGET_LABEL = {'youth': '청년', 'newlywed': '신혼부부'}
LEASE_LABEL = {'monthly': '월세', 'jeonse': '전세'}
def normalize_cfg(cfg, over=None):
    """하위호환: 새 키가 없으면 월세+청년만(기존과 동일). monthly 블록 ↔ 최상위 키 동기화(CLI --max-deposit/--max-rent 는 월세)."""
    m = dict(cfg.get('monthly') or {})
    for k in MONTHLY_KEYS:
        if (over or {}).get(k) is not None: m[k] = over[k]
        elif k not in m and k in cfg: m[k] = cfg[k]
    cfg['monthly'] = m
    for k in MONTHLY_KEYS:
        if k in m: cfg[k] = m[k]
    cfg['lease_types'] = [x for x in (cfg.get('lease_types') or ['monthly']) if x in LEASE_LABEL] or ['monthly']
    cfg['targets'] = [x for x in (cfg.get('targets') or ['youth']) if x in TARGET_LABEL] or ['youth']
    j = dict(cfg.get('jeonse') or {})
    j.setdefault('max_deposit_manwon', 20000); j.setdefault('near_tolerance_deposit_manwon', 5000)
    j.setdefault('jeonse_like_max_rent_manwon', 10); j.setdefault('jeonse_like_min_deposit_manwon', 5000)
    for k, ok in (('jeonse_max_deposit_manwon', 'max_deposit_manwon'),):
        if (over or {}).get(k) is not None: j[ok] = over[k]
    cfg['jeonse'] = j
    if cfg.get('support_programs') is None: cfg['support_programs'] = 'jeonse' in cfg['lease_types']   # 기본: 전세 선택 시 켬
    cfg.setdefault('conversion_rate_pct', None)                  # 표시 전용(판정에 쓰지 않음)
    pg = cfg.setdefault('pages', {}); pg.setdefault('hug', 1)
    legacy = cfg['lease_types'] == ['monthly'] and cfg['targets'] == ['youth'] and not cfg['support_programs']
    pg.setdefault('lh_rolling_days', 0 if legacy else 365)       # LH 연중 수시모집 보강(구 설정은 기존과 동일하게 끔)
    return cfg
def target_set(cfg):
    """판정에 쓰는 unit.target 허용 집합: 선택 대상 + '공통'."""
    return {'공통'} | {TARGET_LABEL[t] for t in cfg.get('targets') or ['youth']}
def target_tag(cfg):
    """알림 태그 접미사: 기본(청년만)이면 빈 문자열 → 기존 notified 태그와 호환."""
    t = cfg.get('targets') or ['youth']
    return '' if t == ['youth'] else '/' + ','.join(sorted(t))
def monthly_tag(cfg): return f"@{cfg['max_deposit_manwon']:g}/{cfg['max_rent_manwon']:g}" + target_tag(cfg)
def jeonse_tag(cfg): return f"@{cfg['jeonse']['max_deposit_manwon']:g}" + target_tag(cfg)
KST = dt.timezone(dt.timedelta(hours=9))
def kst_today(): return dt.datetime.now(KST).date()
def pick_units(units):
    """판정 근거 선택: 사람 확인값이 있으면 그것만, 없으면 텍스트 자동추출(auto), 그것도 없으면 OCR. returns (basis, units)"""
    for basis in ('human', 'auto', 'ocr'):
        us = [u for u in units if (u.get('origin') or 'human') == basis]
        if us: return basis, us
    return 'none', []
SEOUL_GU = ('종로구 중구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구 양천구 강서구 '
            '구로구 금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구').split()
_GU_RE = re.compile(r'(?:^|[^가-힣]|서울(?:특별시)?\s?)(' + '|'.join(sorted(SEOUL_GU, key=len, reverse=True)) + r')(?![가-힣])')
def unit_gu(u):
    """주택형 행의 자치구: units.gu(있으면) > 라벨/비고 속 '마포구'·'서울마포구' 표기. 없으면 None."""
    if u.get('gu'): return u['gu']
    m = _GU_RE.search(f"{u.get('unit_label') or ''} {u.get('note') or ''}")
    return m.group(1) if m else None
def drop_excluded_gu(units, cfg):
    """제외 구(exclude_gu)에 있는 주택형 행을 뺀다 → (남은 행, 뺀 구 목록). 구를 모르는 행은 유지."""
    ex = set(cfg.get('exclude_gu') or ())
    if not ex: return list(units), []
    keep, gone = [], set()
    for u in units:
        g = unit_gu(u)
        if g in ex: gone.add(g)
        else: keep.append(u)
    return keep, sorted(gone)
def is_jeonse_unit(u): return (u.get('lease_type') == 'jeonse') or not u.get('rent')
def judge(units, meta, closed, cfg):
    """월세 판정. returns fit, reason, best_unit, basis.  (전세 표 행(rent=0)은 제외)
    basis=human → 그대로 판정. basis=auto(텍스트 PDF/HWP 표) → 판정은 그대로 쓰되 사유에 [자동추출] 표기(알림에 '원문 확인 권장').
    basis=ocr → 숫자 신뢰 불가: fit='unverified'(자동추출·미검증), 사유에 잠정 판정을 남겨 루틴 에이전트가 크롭 이미지로 확인."""
    basis, units = pick_units([u for u in units if not is_jeonse_unit(u)])
    f, r, b = _judge(units, meta, closed, cfg)
    if basis == 'auto': r = '[자동추출] ' + r
    if basis == 'ocr' and f not in ('excluded_region',):
        r = f'자동추출(미검증·OCR) 잠정 {f}: {r}'; f = 'closed' if closed else 'unverified'
    return f, r, b, basis
def _judge(units, meta, closed, cfg):
    gu = (meta or {}).get('gu') or ''
    if any(g in gu for g in cfg['exclude_gu']): return 'excluded_region', f'제외 지역 {gu}', None
    units, gone = drop_excluded_gu(units, cfg)            # 주택형 단위 제외 구(라벨에 구가 있는 행)
    if gone and not units: return 'excluded_region', f"제외 지역 {'·'.join(gone)}(모든 주택형)", None
    ok_t = target_set(cfg); us = [u for u in units if (u['target'] or '공통') in ok_t]
    md, mr = cfg['max_deposit_manwon'], cfg['max_rent_manwon']
    nd, nr = md + cfg['near_tolerance_deposit_manwon'], mr + cfg['near_tolerance_rent_manwon']
    if units and not us:
        msg = '청년 대상 주택형 없음(신혼부부형 등)' if ok_t == {'공통', '청년'} else f"{'·'.join(sorted(ok_t - {'공통'}))} 대상 주택형 없음"
        return ('closed' if closed else 'no'), msg, None
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

def jeonse_like(u, cfg):
    """월세 옵션이지만 전세에 가까운지(🔁 전환): 월세 ≤ jeonse_like_max_rent 이고 보증금 ≥ jeonse_like_min_deposit."""
    j = cfg['jeonse']
    return (not is_jeonse_unit(u)) and u['rent'] <= j['jeonse_like_max_rent_manwon'] and u['deposit'] >= j['jeonse_like_min_deposit_manwon']
def judge_jeonse(units, meta, closed, cfg, notice_jeonse=False):
    """전세 판정. 후보 = 전세 표 행(rent=0) + 전세에 가까운 월세 옵션(🔁, best['conv']=True).
    returns (fit, reason, best, basis) 또는 후보가 없고 전세 공고도 아니면 None(해당 없음)."""
    cand = [dict(u, conv=not is_jeonse_unit(u)) for u in units if is_jeonse_unit(u) or jeonse_like(u, cfg)]
    if not cand and not notice_jeonse: return None
    basis, us = pick_units(cand)
    gu = (meta or {}).get('gu') or ''
    if any(g in gu for g in cfg['exclude_gu']): return 'excluded_region', f'제외 지역 {gu}', None, basis
    us, gone = drop_excluded_gu(us, cfg)
    if gone and not us: return 'excluded_region', f"제외 지역 {'·'.join(gone)}(모든 주택형)", None, basis
    ok_t = target_set(cfg); us2 = [u for u in us if (u['target'] or '공통') in ok_t]
    if us and not us2: return ('closed' if closed else 'no'), f"{'·'.join(sorted(ok_t - {'공통'}))} 대상 전세 주택형 없음", None, basis
    if not us2: return ('closed' if closed else 'unknown'), '전세 주택형·보증금 미확인(공고문 확인 필요)', None, basis
    j = cfg['jeonse']; jd = j['max_deposit_manwon']; nd = jd + j['near_tolerance_deposit_manwon']
    def key(u): return (u['deposit'] / u['area_m2']) if u['area_m2'] else 9e9
    def desc(u): return f"{u['unit_label']} 전세 {u['deposit']:g}만" + (f" (월 {u['rent']:g}만, 전환 옵션)" if u['conv'] else '')
    m = [u for u in us2 if u['deposit'] <= jd]; n = [u for u in us2 if u['deposit'] <= nd]
    if m: b = min(m, key=key); f, r = ('closed_match' if closed else 'match'), desc(b)
    elif n: b = min(n, key=key); f, r = ('closed' if closed else 'near'), '근소초과 ' + desc(b)
    else: b = min(us2, key=lambda u: u['deposit']); f, r = ('closed' if closed else 'no'), f"조건 밖 (최저 전세 {b['deposit']:g}만)"
    if basis == 'auto': r = '[자동추출] ' + r
    if basis == 'ocr' and f != 'excluded_region': r = f'자동추출(미검증·OCR) 잠정 {f}: {r}'; f = 'closed' if closed else 'unverified'
    return f, r, b, basis
FIT_RANK = {'match': 0, 'closed_match': 1, 'near': 2, 'unverified': 3, 'unknown': 4, 'program': 5, 'closed': 6, 'no': 7, 'excluded_region': 8, 'irrelevant': 9}
