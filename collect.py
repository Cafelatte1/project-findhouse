#!/usr/bin/env python3
"""수집 → 정규화/중복제거 → 판정 → housing.db 스냅샷 저장 (같은 collected_date 재실행 시 upsert).
사용: python collect.py [--max-deposit N] [--max-rent N] [--date YYYY-MM-DD] [--only sh,lh]"""
import argparse, json, re, hashlib, time, datetime as dt, traceback
import hdb, sources, docs, addr

RELEVANT = re.compile(r'청년|행복주택|안심주택|매입임대|사회주택|도시형생활|원룸|1~2인|잔여세대|토지지원|토지임대부|장기미임대|재개발임대|국민임대|통합공공')
# (구) EXCLUDE 하나에 섞여 있던 규칙을 셋으로 분리 — 판정은 제목·소스 카테고리만 사용(본문 키워드 금지: 월세 공고문에도 '전세대 전액보증' 등이 흔함)
NOISE = re.compile(r'접수결과|접수\s*결과|결과\s*(및|안내)|가능\s*단지\s*안내|접수마감|청약마감|당첨자|예비\d*차|발표|경쟁률|계약안내|계약결과|입주안내|안내문|재계약|서비스|일시중단|일정 연기')   # 결과·안내 공지
ALWAYS_EX = re.compile(r'다자녀|고령자|희망하우징|기숙사|연극인|예술인|육아|두레주택|가정어린이집')                                    # 대상 밖(항상 제외)
NEWLYWED_ONLY_EX = re.compile(r'공공한옥')                       # 신혼부부 대상(미리내집 공공한옥) — 신혼부부를 고르지 않으면 제외
NEWLYWED = re.compile(r'신혼|신생아|미리내집')
YOUTH = re.compile(r'청년|대학생')
SUPPORT = re.compile(r'전세임대')                                 # 입주자가 집을 구해오는 지원형 (전세임대형 든든주택 포함)
JEONSE = re.compile(r'장기전세|든든전세|전세형|전세주택')

def lease_of(it):
    """공고 수준 임대유형: monthly | jeonse | support (제목·카테고리·소스 지정만)."""
    if it.get('lease'): return it['lease']
    t, cat = it.get('title') or '', it.get('category') or ''
    if SUPPORT.search(t) or cat == '전세임대': return 'support'
    if JEONSE.search(t) or cat in ('장기전세주택', '든든전세주택'): return 'jeonse'
    return 'monthly'

def notice_targets(it):
    """제목·카테고리에 명시된 대상 {'youth','newlywed'}. 빈 집합 = 일반 공고 → 주택형 표의 공급대상으로 판단.
    둘 다 = 혼합 공고('청년·신혼부부', '청년 및 신혼부부') → 어느 한쪽만 골라도 포함(주택형 행은 선택 대상만 판정).
    '청년안심주택'은 사업명이라 대상 표기로 보지 않는다(신혼부부형도 있음)."""
    t = re.sub(r'청년안심주택', '', (it.get('title') or '') + ' ' + (it.get('category') or ''))
    return ({'newlywed'} if NEWLYWED.search(t) else set()) | ({'youth'} if YOUTH.search(t) else set())

def elig_note(it, lease, meta):
    """알림용 자격 한 줄: 사람 기록 > 원문에서 읽은 문구(HUG 등) > 세대 기준 공고 유형 안내."""
    if meta.get('eligibility'): return meta['eligibility']
    if it.get('elig'): return it['elig']
    t = (it.get('title') or '') + ' ' + (it.get('category') or '')
    if lease in ('jeonse', 'support') or re.search(r'국민임대|재개발임대|영구임대|신혼|신생아|미리내집', t):
        return '세대 기준 공고(무주택세대구성원 등) — 자격 원문 확인'
    return None

def norm_title(t):
    t = re.sub(r'\(?\s*\d{2,4}\s*[.\-]\s*\d{1,2}\s*[.\-]\s*\d{1,2}\.?\s*\)?', '', t or '')   # 날짜 제거
    t = re.sub(r'\[(정정|수정)[^\]]*\]|\((정정|수정)\)|공고문|공고|모집|입주자|\s|[\[\]()_·,.]', '', t)
    return t

def is_relevant(it, cfg=None):
    """선택한 임대유형(lease_types)·대상(targets)·지원 프로그램(support_programs) 기준 관련성.
    설정에 새 키가 없으면(월세+청년) 구 EXCLUDE 규칙과 같은 결과 — 단, 청년+신혼 혼합 제목 공고는 포함(의도적 변경, selftest 고정)."""
    cfg = cfg or {}
    lt = cfg.get('lease_types') or ['monthly']; tg = cfg.get('targets') or ['youth']
    nw, yo = 'newlywed' in tg, 'youth' in tg
    t = it.get('title') or ''; src = it['source']
    lease = lease_of(it); nt = notice_targets(it)
    if lease == 'support':
        if not cfg.get('support_programs'): return False
    elif lease == 'jeonse':
        if 'jeonse' not in lt: return False
    elif not lt: return False          # monthly 공고: 월세 선택 시 + 전세 선택 시(🔁 전세에 가까운 옵션 탐색)
    # 대상 표기: 한 대상 전용 공고는 그 대상을 골랐을 때만, 혼합(둘 다 표기)·일반(표기 없음)은 항상 → 주택형 행으로 거름.
    # (구 동작과 다른 점: 청년만 골라도 '청년·신혼부부' 혼합 제목 공고를 포함 — 의도적 변경)
    if nt == {'newlywed'} and not nw: return False
    if nt == {'youth'} and not yo: return False
    if src == 'soco':                  # 청년안심주택: 청년·신혼부부 주택형 혼합 → 주택형 표로 거름
        return lease == 'monthly' or lease in lt
    if NOISE.search(t) or ALWAYS_EX.search(t): return False
    if not nw and NEWLYWED_ONLY_EX.search(t): return False
    if src == 'socialhousing': return it.get('status') == '모집중' and '서울' in (it.get('address') or '')
    if src == 'lh' and it.get('category') in ('행복주택',): return True
    if lease != 'monthly' or src == 'hug': return True
    return bool(RELEVANT.search(t)) or bool(NEWLYWED.search(t) and nw)

def dedup(items):
    """같은 소스 내 재게시(다른 seq, 같은 제목) → 최신 posted 1건만 대표. seoulportal 은 SH 게시글과 제목이 같으면 상태만 SH에 병합."""
    best = {}
    for it in items:
        k = (it['source'] if it['source'] != 'seoulportal' else 'sh', norm_title(it['title']))
        it['dedup_key'] = k[1]
        cur = best.get(k)
        if cur is None: best[k] = it; continue
        if it['source'] == 'seoulportal' and cur['source'] == 'sh':
            cur['status'] = cur.get('status') or it['status']; cur['portal_status'] = it['status']; it['dup_of'] = f"sh:{cur['item_id']}"; continue
        if cur['source'] == 'seoulportal' and it['source'] == 'sh':
            it['status'] = it.get('status') or cur['status']; it['portal_status'] = cur['status']; cur['dup_of'] = f"sh:{it['item_id']}"; best[k] = it; continue
        if (it.get('posted') or '') > (cur.get('posted') or '') or ((it.get('posted') or '') == (cur.get('posted') or '') and it['item_id'] > cur['item_id']):
            cur['dup_of'] = f"{it['source']}:{it['item_id']}"; best[k] = it
        else:
            it['dup_of'] = f"{cur['source']}:{cur['item_id']}"
    return items

def row_hash(it):
    return hashlib.sha1(json.dumps([it.get(k) for k in ('title', 'status', 'apply_start', 'apply_end')], ensure_ascii=False).encode()).hexdigest()[:12]

def run(over=None, date=None, only=None):
    cfg = hdb.load_cfg(over); con = hdb.connect()
    today = dt.date.fromisoformat(date) if date else hdb.kst_today(); cd = today.isoformat()
    t0 = time.time(); started = dt.datetime.now(hdb.KST).isoformat(timespec='seconds')
    items, counts, errors, src_sec = [], {}, {}, {}
    enabled = set(cfg['lease_types']) | ({'support'} if cfg.get('support_programs') else set())
    for name, fn in sources.ALL.items():
        if only and name not in only: continue
        if getattr(sources, 'NEEDS', {}).get(name) and sources.NEEDS[name] not in enabled: continue
        ts = time.time()
        try:
            r = fn(cfg); counts[name] = len(r); items += r
        except Exception as e:                       # 소스 하나 실패해도 계속
            counts[name] = 0; errors[name] = f'{type(e).__name__}: {e}'[:300]
        src_sec[name] = round(time.time() - ts, 1)
    seen = set(); uniq = []
    for it in items:                                  # 서로 다른 수집 함수가 같은 공고(source,item_id)를 돌려준 경우 1건만
        k = (it['source'], it['item_id'])
        if k not in seen: seen.add(k); uniq.append(it)
    items = uniq
    dedup(items)
    cur = con.execute('INSERT INTO runs(collected_date,started_at,config_json,counts_json,errors_json,status,max_deposit_manwon,max_rent_manwon) VALUES(?,?,?,?,?,?,?,?)',
                      (cd, started, json.dumps(cfg, ensure_ascii=False), json.dumps(counts), json.dumps(errors, ensure_ascii=False), 'running', cfg['max_deposit_manwon'], cfg['max_rent_manwon']))
    run_id = cur.lastrowid
    doc_budget, doc_log = [cfg.get('docs', {}).get('max_items_per_run', 20)], []
    nd = cfg['max_deposit_manwon'] + cfg['near_tolerance_deposit_manwon']; nr = cfg['max_rent_manwon'] + cfg['near_tolerance_rent_manwon']
    for it in items:
        if it.get('dup_of'): continue
        src, iid = it['source'], it['item_id']
        reg = con.execute('SELECT * FROM items WHERE source=? AND item_id=?', (src, iid)).fetchone()
        repost_of = None
        if reg is None:   # 재게시(새 seq, 같은 제목) → 이전 seq 의 기록/알림상태 승계
            old = con.execute('SELECT * FROM items WHERE source=? AND dedup_key=? AND item_id<>? ORDER BY last_seen_date DESC LIMIT 1', (src, it['dedup_key'], iid)).fetchone()
            if old:
                repost_of = old['item_id']
                MC = 'name,gu,station,housing_type,apply_start,apply_end,supply_count,eligibility,note,verified_at,address'
                con.execute(f'INSERT OR IGNORE INTO item_meta(source,item_id,{MC}) SELECT source,?,{MC} FROM item_meta WHERE source=? AND item_id=?', (iid, src, old['item_id']))
                if not con.execute('SELECT 1 FROM units WHERE source=? AND item_id=?', (src, iid)).fetchone():
                    UC = 'unit_label,target,area_m2,deposit,rent,note,origin,verified,supply,doc_id,lease_type,address'
                    con.execute(f'INSERT INTO units(source,item_id,{UC}) SELECT source,?,{UC} FROM units WHERE source=? AND item_id=?', (iid, src, old['item_id']))
                con.execute('INSERT INTO items(source,item_id,first_seen_date,last_seen_date,last_hash,notified_fit,dedup_key,address) VALUES(?,?,?,?,?,?,?,?)', (src, iid, old['first_seen_date'], cd, old['last_hash'], old['notified_fit'], it['dedup_key'], old['address']))
                reg = con.execute('SELECT * FROM items WHERE source=? AND item_id=?', (src, iid)).fetchone()
        meta = con.execute('SELECT * FROM item_meta WHERE source=? AND item_id=?', (src, iid)).fetchone()
        meta = dict(meta) if meta else {}
        if not meta.get('gu') and it.get('gu'): meta['gu'] = it['gu']
        units = [dict(u) for u in con.execute('SELECT * FROM units WHERE source=? AND item_id=?', (src, iid))]
        apply_start = meta.get('apply_start') or it.get('apply_start')
        apply_end = meta.get('apply_end') or it.get('apply_end')
        if src == 'soco' and not apply_end and apply_start:      # 목록엔 신청 시작일만 → 가정 창
            apply_end = (dt.date.fromisoformat(apply_start) + dt.timedelta(days=cfg['soco_assumed_window_days'])).isoformat()
        st = it.get('status') or ''
        end_d = None
        try: end_d = dt.date.fromisoformat(apply_end) if apply_end and re.match(r'\d{4}-\d\d-\d\d$', apply_end) else None
        except ValueError: pass
        closed = st in ('모집마감', '접수마감') or (end_d is not None and end_d < today)
        if src == 'seoulportal' and st == '모집중' and not meta.get('apply_end'): st = '포털 모집중(접수기간 확인 필요)'
        status = st or ('마감' if closed else ('접수예정' if apply_start and apply_start > cd else ('모집중(추정)' if end_d or apply_start else '확인 필요')))
        relevant = is_relevant(it, cfg); lease = lease_of(it); ntg = notice_targets(it)
        dc = cfg.get('docs', {}); auto_addr = None          # 공고 단위 자동 주소(이번 실행에서 새로 얻은 값; None=변화 없음, ''=찾아봤지만 없음)
        if relevant and lease != 'support' and not closed and dc.get('enabled', True) and src in docs.SUPPORTED and doc_budget[0] > 0:   # 사람 기록이 있어도 1회 처리(누락 옵션 탐지용)
            try:
                t1 = time.time(); r = docs.process_item(con, src, iid, it['url'], cfg, **({'lease_hint': 'jeonse'} if lease == 'jeonse' else {}))
                if r.get('address'): auto_addr = r['address']
                if not r.get('skipped'):
                    doc_budget[0] -= 1; doc_log.append(dict(item=f'{src}:{iid}', sec=round(time.time() - t1, 1), **r))
                units = [dict(u) for u in con.execute('SELECT * FROM units WHERE source=? AND item_id=?', (src, iid))]
            except Exception as e:
                doc_log.append(dict(item=f'{src}:{iid}', error=f'{type(e).__name__}: {e}'[:200]))
        if relevant and src == 'socialhousing' and it.get('address'): auto_addr = addr.find(it['address']) or auto_addr   # 목록 '주소' 열
        if relevant and src == 'lh' and not closed and (reg is None or reg['address'] is None) and not auto_addr:
            auto_addr = sources.lh_address(it, cfg) or ''      # LH 상세 '소재지'(단일 단지) — 공고당 1회만 조회
        fm = fj = None; bm = bj = None; rm = rj = None; basis = 'none'
        if not relevant:
            fit, reason, best, basis = 'irrelevant', '키워드 필터 제외', None, 'none'
        elif lease == 'support':                   # 지원 프로그램: 공고 단위, 가격 판정 없음
            fit, reason, best = ('closed' if closed else 'program'), '지원 프로그램(입주자가 주택을 구해 신청) — 가격 판정 없음', None
        else:
            has_monthly_units = any(not hdb.is_jeonse_unit(u) for u in units)
            if 'monthly' in cfg['lease_types'] and (lease == 'monthly' or has_monthly_units):
                fm, rm, bm, bsm = hdb.judge(units, meta, closed, cfg)
                if bsm == 'human':                 # 사람 기록에 없는 더 유리한 옵션이 첨부 표에 있으면 사유에 표시(판정은 사람 값 유지)
                    au = [u for u in units if u.get('origin') == 'auto' and not hdb.is_jeonse_unit(u)]
                    if au:
                        af, ar, _ = hdb._judge(au, meta, closed, cfg); rk = {'match': 0, 'closed_match': 0, 'near': 1}
                        if rk.get(af, 9) < rk.get(fm, 9): rm += f' ※첨부 자동추출에 더 유리한 옵션({af}): {ar} → 원문 확인 후 units.py 기록'
                if closed and fm == 'unknown': fm = 'closed'
                basis = bsm
            if 'jeonse' in cfg['lease_types']:
                jr = hdb.judge_jeonse(units, meta, closed, cfg, notice_jeonse=(lease == 'jeonse'))
                if jr:
                    fj, rj, bj, bsj = jr
                    if closed and fj == 'unknown': fj = 'closed'
                    if fm is None: basis = bsj
            cands = [(f, r, b) for f, r, b in ((fm, rm, bm), (fj, rj, bj)) if f is not None]
            if cands:
                fit, reason, best = min(cands, key=lambda x: hdb.FIT_RANK.get(x[0], 9))   # 월세 우선(동률 시)
            else:
                fit, reason, best = ('closed' if closed else ('unknown' if not units else 'no')), '선택한 임대유형에 해당하는 옵션 없음', None
        item_addr = auto_addr or (reg['address'] if reg is not None and reg['address'] else None)
        gu0 = meta.get('gu') or it.get('gu')
        best_json = json.dumps({k: (dict({x: b.get(x) for x in ('unit_label', 'target', 'area_m2', 'deposit', 'rent', 'conv', 'origin')},
                                         address=addr.pick(b, units, meta, item_addr, gu0)) if b else None)
                                for k, b in (('monthly', bm), ('jeonse', bj))}, ensure_ascii=False) if relevant else None
        laddr = addr.pick(best, units, meta, item_addr, gu0) if relevant else None
        enote = elig_note(it, lease, meta) if relevant else None
        due = int(bool(relevant and not closed and end_d and 0 <= (end_d - today).days <= cfg['due_soon_days']))
        if relevant and fit == 'program' and status in ('확인 필요',) and it.get('status'): status = it['status']
        it2 = dict(it, apply_start=apply_start, apply_end=apply_end, status=status); h = row_hash(it2)
        if reg is None:
            con.execute('INSERT INTO items(source,item_id,first_seen_date,last_seen_date,last_hash,dedup_key) VALUES(?,?,?,?,?,?)', (src, iid, cd, cd, h, it['dedup_key'])); first = cd
        else:
            first = reg['first_seen_date']; con.execute('UPDATE items SET last_seen_date=?, dedup_key=? WHERE source=? AND item_id=?', (cd, it['dedup_key'], src, iid))
        if auto_addr is not None: con.execute('UPDATE items SET address=? WHERE source=? AND item_id=?', (auto_addr, src, iid))
        prev = con.execute('SELECT hash FROM listings WHERE source=? AND item_id IN (?,?) AND collected_date<? ORDER BY collected_date DESC LIMIT 1', (src, iid, repost_of or iid, cd)).fetchone()
        is_new = int(first == cd and not repost_of); is_changed = int(bool(repost_of) or bool(prev and prev['hash'] != h))
        area = best['area_m2'] if best else None; dep = best['deposit'] if best else None; rent = best['rent'] if best else None
        rpm = int(round(rent * 10000 / area)) if best and area else None
        con.execute('''INSERT INTO listings(collected_date,source,item_id,run_id,title,category,name,gu,station,area_m2,deposit,rent,rent_per_m2_won,
            posted,apply_start,apply_end,status,relevant,fit,fit_reason,fit_basis,crit_max_deposit,crit_max_rent,crit_near_deposit,crit_near_rent,
            is_new,is_changed,is_due_soon,url,hash,updated_at,lease_type,targets,fit_monthly,reason_monthly,fit_jeonse,reason_jeonse,best_json,elig_note,extra_note,address)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(collected_date,source,item_id) DO UPDATE SET run_id=excluded.run_id,title=excluded.title,category=excluded.category,name=excluded.name,
            gu=excluded.gu,station=excluded.station,area_m2=excluded.area_m2,deposit=excluded.deposit,rent=excluded.rent,rent_per_m2_won=excluded.rent_per_m2_won,
            posted=excluded.posted,apply_start=excluded.apply_start,apply_end=excluded.apply_end,status=excluded.status,relevant=excluded.relevant,fit=excluded.fit,
            fit_reason=excluded.fit_reason,fit_basis=excluded.fit_basis,crit_max_deposit=excluded.crit_max_deposit,crit_max_rent=excluded.crit_max_rent,crit_near_deposit=excluded.crit_near_deposit,
            crit_near_rent=excluded.crit_near_rent,is_new=excluded.is_new,is_changed=excluded.is_changed,is_due_soon=excluded.is_due_soon,url=excluded.url,
            hash=excluded.hash,updated_at=excluded.updated_at,lease_type=excluded.lease_type,targets=excluded.targets,fit_monthly=excluded.fit_monthly,
            reason_monthly=excluded.reason_monthly,fit_jeonse=excluded.fit_jeonse,reason_jeonse=excluded.reason_jeonse,best_json=excluded.best_json,
            elig_note=excluded.elig_note,extra_note=excluded.extra_note,address=excluded.address''',
            (cd, src, iid, run_id, it['title'], it.get('category'), meta.get('name'), meta.get('gu'), meta.get('station'), area, dep, rent, rpm,
             it.get('posted'), apply_start, apply_end, status, int(relevant), fit, reason, basis, cfg['max_deposit_manwon'], cfg['max_rent_manwon'], nd, nr,
             is_new, is_changed, due, it.get('url'), h, dt.datetime.now(hdb.KST).isoformat(timespec='seconds'),
             lease, ','.join(hdb.TARGET_LABEL[x] for x in ('youth', 'newlywed') if x in ntg), fm, rm, fj, rj, best_json, enote, it.get('extra_note'), laddr))
        con.execute('UPDATE items SET last_hash=? WHERE source=? AND item_id=?', (h, src, iid))
    # 이번 실행에서 대표가 아닌(중복) 행은 같은 날 이전 실행분에서 남아있으면 제거 → 멱등
    dups = [(cd, i['source'], i['item_id']) for i in items if i.get('dup_of')]
    con.executemany('DELETE FROM listings WHERE collected_date=? AND source=? AND item_id=?', dups)
    elapsed = round(time.time() - t0, 1)
    con.execute('UPDATE runs SET finished_at=?,status=?,counts_json=? WHERE run_id=?', (dt.datetime.now(hdb.KST).isoformat(timespec='seconds'), 'ok' if not errors else 'partial',
                json.dumps(dict(counts, _docs=len(doc_log), _elapsed_sec=elapsed, _source_sec=src_sec), ensure_ascii=False), run_id))
    con.commit()
    return dict(run_id=run_id, collected_date=cd, counts=counts, errors=errors, elapsed_sec=elapsed, source_sec=src_sec, docs=doc_log, cfg=cfg)

def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-deposit', type=float, help='월세: 보증금 상한(만원)'); ap.add_argument('--max-rent', type=float, help='월세: 월세 상한(만원)')
    ap.add_argument('--jeonse-max-deposit', type=float, help='전세: 보증금 상한(만원)')
    ap.add_argument('--types', help='임대유형 덮어쓰기 예: monthly,jeonse'); ap.add_argument('--targets', help='대상 덮어쓰기 예: youth,newlywed')
    ap.add_argument('--date', help='수집기준일자 강제(YYYY-MM-DD, 기본 오늘 KST)'); ap.add_argument('--only', help='소스 제한 예: sh,lh')
    return ap.parse_args(argv)
def overrides(a):
    o = {'max_deposit_manwon': a.max_deposit, 'max_rent_manwon': a.max_rent, 'jeonse_max_deposit_manwon': getattr(a, 'jeonse_max_deposit', None)}
    if getattr(a, 'types', None): o['lease_types'] = a.types.split(',')
    if getattr(a, 'targets', None): o['targets'] = a.targets.split(',')
    return o
if __name__ == '__main__':
    a = parse_args(); r = run(overrides(a), a.date, a.only.split(',') if a.only else None); r.pop('cfg'); print(json.dumps(r, ensure_ascii=False))
