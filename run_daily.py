#!/usr/bin/env python3
"""단일 진입점: 수집 + DB 저장 + 판정 + 알림 마크다운 출력.
  python run_daily.py [--max-deposit N] [--max-rent N] [--jeonse-max-deposit N] [--types monthly,jeonse]
                      [--targets youth,newlywed] [--format chat|cards] [--summary] [--no-mark]
알릴 것(임대유형별 블록 — 월세/전세 각각): (1) 새로 부합/근소초과 (2) 마감 임박(due_soon_days)
        (3) 가격 미확인 새 공고(이미지형 공고는 페이지 PNG 경로 포함 → 에이전트가 직접 읽음) (4) OCR 미검증 잠정 match/near (docs.ocr=true 일 때만)
        + 📋 지원 프로그램(전세임대: 새 공고·마감 임박만) + 수집처 장애. 없으면 '알릴 것 없음'.
알림 상태: 월세 items.notified_fit(태그 '@보증금/월세[/대상]', 기존 값과 호환) · 전세 items.notified_jeonse · 지원 items.notified_support."""
import argparse, json, re
import hdb, collect, report, sources

ALERT_FITS = ('match', 'near', 'unknown', 'unverified')

def _crops(con, r, method=None, status=None):
    out = []
    q = 'SELECT summary, structured_json FROM documents WHERE source=? AND item_id=?' + (" AND method='tesseract-ocr'" if method else '') + (" AND status='needs_agent'" if status else '')
    for d in con.execute(q, (r['source'], r['item_id'])):
        if method:
            try: out += json.loads(d['summary'] or '{}').get('crops') or []
            except Exception: pass
        try:
            sj = json.loads(d['structured_json'] or '{}')
            out += (sj.get('crops') or []) if method else (sj.get('pages_rendered') or [])
        except Exception: pass
    return out

def block(con, rows, fcol, ncol, tag, rcol):
    """임대유형 하나의 알림 섹션. fcol=해당 유형 판정 컬럼, ncol=알림상태 컬럼."""
    rs = [r for r in rows if r.get(fcol)]
    newfit = [r for r in rs if r[fcol] in ('match', 'near') and (r[ncol] or '') != r[fcol] + tag]
    shown = {(r['source'], r['item_id']) for r in newfit}
    due = [r for r in rs if r['is_due_soon'] and r[fcol] in ALERT_FITS and (r['source'], r['item_id']) not in shown]
    unver = [r for r in rs if r[fcol] == 'unverified' and re.search(r'잠정 (match|near)', r.get(rcol) or '') and (r[ncol] or '') != 'unverified' + tag]
    for r in unver: r['crops'] = [_crops(con, r, method='ocr')]
    unknown = [r for r in rs if r[fcol] == 'unknown' and (r[ncol] or '') != 'unknown' + tag]
    for r in unknown:
        pages = _crops(con, r, status='needs_agent')
        if pages: r['crops'] = [pages]
    notice = [r for r in unknown if r['source'] in getattr(sources, 'NOTICE_LEVEL', ())]   # 공고 단위 수집처(주택별 가격 없음)
    return dict(match=[r for r in newfit if r[fcol] == 'match'], near=[r for r in newfit if r[fcol] == 'near'],
                due=due, notice=notice, unknown=[r for r in unknown if r not in notice], unverified=unver), newfit + unver + unknown

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--max-deposit', type=float); ap.add_argument('--max-rent', type=float)
    ap.add_argument('--jeonse-max-deposit', type=float); ap.add_argument('--types'); ap.add_argument('--targets')
    ap.add_argument('--format', choices=('chat', 'cards'), default='chat', help='chat=채팅 블록형(기본), cards=카드형(레거시)')
    ap.add_argument('--summary', action='store_true'); ap.add_argument('--no-mark', action='store_true')
    ap.add_argument('--date'); a = ap.parse_args()
    res = collect.run(collect.overrides(a), a.date)
    cfg, cd = res['cfg'], res['collected_date']; con = hdb.connect()
    mtag, jtag = hdb.monthly_tag(cfg), hdb.jeonse_tag(cfg)
    rows = [dict(r) for r in con.execute('''SELECT l.*, i.notified_fit, i.notified_jeonse, i.notified_support FROM listings l JOIN items i USING(source,item_id)
                                            WHERE l.collected_date=? AND l.relevant=1 ORDER BY COALESCE(l.rent_per_m2_won,1e12)''', (cd,))]
    blocks, marks = {}, []
    if 'monthly' in cfg['lease_types']:
        blocks['monthly'], mk = block(con, rows, 'fit_monthly', 'notified_fit', mtag, 'reason_monthly'); marks += [('notified_fit', mtag, 'fit_monthly', r) for r in mk]
    if 'jeonse' in cfg['lease_types']:
        jrows = sorted(rows, key=lambda r: ((json.loads(r.get('best_json') or '{}') or {}).get('jeonse') or {}).get('deposit') or 1e12)
        blocks['jeonse'], mk = block(con, jrows, 'fit_jeonse', 'notified_jeonse', jtag, 'reason_jeonse'); marks += [('notified_jeonse', jtag, 'fit_jeonse', r) for r in mk]
    support = [r for r in rows if r['fit'] == 'program' and (not r['notified_support'] or r['is_due_soon'])] if cfg.get('support_programs') else []
    fails = dict(res['errors'] or {})
    if a.format == 'cards':      # 레거시 카드형: 월세 기준 섹션(구 출력과 동일) + 전세 섹션은 제목에 '전세' 표기
        m = blocks.get('monthly') or {}
        sections = [('match', m.get('match')), ('near', m.get('near')), ('due', [r for r in rows if r['is_due_soon'] and r.get('fit_monthly') in ALERT_FITS]),
                    ('unverified', m.get('unverified')), ('unknown', m.get('unknown'))]
        j = blocks.get('jeonse') or {}
        for k in ('match', 'near', 'unknown'):
            if j.get(k): report.SECTION['jeonse_' + k] = ('전세 ' + report.SECTION[k][0], report.SECTION[k][1]); sections.append(('jeonse_' + k, j[k]))
        if support: report.SECTION['support'] = ('📋 지원 프로그램', '전세임대 등 — 가격 판정 없음'); sections.append(('support', support))
        sections.append(('fail', fails))
        if a.summary: sections.append(('summary', [r for r in rows if r['fit'] in ('match', 'near')]))
        print(report.render_alert(cd=cd, cfg=cfg, res=res, sections=sections))
    else:
        print(report.render_chat(cd=cd, cfg=cfg, blocks=blocks, support=support, fails=fails))
        if a.summary:
            print('\n' + report.render_query(cd, [r for r in rows if r['fit'] in ('match', 'near')], title='오늘 부합·근소초과 전체'))
    if not a.no_mark:
        for col, tag, fcol, r in marks:
            val = ('unverified' if r[fcol] == 'unverified' else r[fcol]) + tag
            con.execute(f'UPDATE items SET {col}=? WHERE source=? AND item_id=?', (val, r['source'], r['item_id']))
        for r in support: con.execute('UPDATE items SET notified_support=? WHERE source=? AND item_id=?', ('program@' + cd, r['source'], r['item_id']))
        con.commit()
if __name__ == '__main__': main()
