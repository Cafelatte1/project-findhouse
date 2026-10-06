#!/usr/bin/env python3
"""단일 진입점: 수집 + DB 저장 + 판정 + 알림 마크다운 출력.
  python run_daily.py [--max-deposit N] [--max-rent N] [--summary] [--no-mark]
알릴 것: (1) 새로 부합/근소초과 (2) 마감 임박(due_soon_days) (3) OCR 미검증 잠정 match/near (docs.ocr=true 일 때만)
        (4) 가격 미확인 새 공고(이미지형 공고는 페이지 PNG 경로 포함 → 에이전트가 직접 읽음) (5) 수집처 장애. 없으면 '알릴 것 없음'."""
import argparse, json, re
import hdb, collect, report

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--max-deposit', type=float); ap.add_argument('--max-rent', type=float)
    ap.add_argument('--summary', action='store_true'); ap.add_argument('--no-mark', action='store_true')
    ap.add_argument('--date'); a = ap.parse_args()
    res = collect.run({'max_deposit_manwon': a.max_deposit, 'max_rent_manwon': a.max_rent}, a.date)
    cfg, cd = res['cfg'], res['collected_date']; con = hdb.connect(); tag = f"@{cfg['max_deposit_manwon']:g}/{cfg['max_rent_manwon']:g}"
    rows = [dict(r) for r in con.execute('''SELECT l.*, i.notified_fit FROM listings l JOIN items i USING(source,item_id)
                                            WHERE l.collected_date=? AND l.relevant=1 ORDER BY COALESCE(l.rent_per_m2_won,1e12)''', (cd,))]
    newfit = [r for r in rows if r['fit'] in ('match', 'near') and (r['notified_fit'] or '') != r['fit'] + tag]
    due = [r for r in rows if r['is_due_soon'] and r['fit'] in ('match', 'near', 'unknown', 'unverified')]
    unver = [r for r in rows if r['fit'] == 'unverified' and re.search(r'잠정 (match|near)', r['fit_reason'] or '') and (r['notified_fit'] or '') != 'unverified' + tag]
    for r in unver:
        crops = []
        for d in con.execute("SELECT summary, structured_json FROM documents WHERE source=? AND item_id=? AND method='tesseract-ocr'", (r['source'], r['item_id'])):
            s = json.loads(d['summary'] or '{}'); crops += s.get('crops') or []
            if d['structured_json']:
                try: crops += json.loads(d['structured_json']).get('crops') or []
                except Exception: pass
        r['crops'] = [crops]
    unknown_new = [r for r in rows if r['fit'] == 'unknown' and (r['notified_fit'] or '') != 'unknown' + tag]
    for r in unknown_new:   # 이미지형 공고(OCR 끔): 에이전트가 볼 페이지 PNG 경로 첨부
        pages = []
        for d in con.execute("SELECT structured_json FROM documents WHERE source=? AND item_id=? AND status='needs_agent'", (r['source'], r['item_id'])):
            try: pages += (json.loads(d['structured_json'] or '{}').get('pages_rendered') or [])
            except Exception: pass
        if pages: r['crops'] = [pages]
    fails = dict(res['errors'] or {})
    sections = [
        ('match', [r for r in newfit if r['fit'] == 'match']),
        ('near', [r for r in newfit if r['fit'] == 'near']),
        ('due', due),
        ('unverified', unver),
        ('unknown', unknown_new),
        ('fail', fails),
    ]
    if a.summary:
        sections.append(('summary', [r for r in rows if r['fit'] in ('match', 'near')]))
    print(report.render_alert(cd=cd, cfg=cfg, res=res, sections=sections))
    if not a.no_mark:
        for r in unver: con.execute('UPDATE items SET notified_fit=? WHERE source=? AND item_id=?', ('unverified' + tag, r['source'], r['item_id']))
        for r in unknown_new: con.execute('UPDATE items SET notified_fit=? WHERE source=? AND item_id=?', ('unknown' + tag, r['source'], r['item_id']))
        for r in newfit: con.execute('UPDATE items SET notified_fit=? WHERE source=? AND item_id=?', (r['fit'] + tag, r['source'], r['item_id']))
        con.commit()
if __name__ == '__main__': main()
