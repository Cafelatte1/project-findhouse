#!/usr/bin/env python3
"""수집기준일자별 조회.  python query.py --date YYYY-MM-DD [--match-only|--fit match,near] [--type monthly|jeonse|support] [--target 청년|신혼부부] [--all] [--runs] [--json]
--type monthly/jeonse 를 주면 --fit 은 해당 유형 판정(fit_monthly/fit_jeonse)에 적용된다."""
import argparse, json, hdb
ap = argparse.ArgumentParser()
ap.add_argument('--date', help='collected_date (기본: 최신)'); ap.add_argument('--match-only', action='store_true')
ap.add_argument('--fit', help='쉼표구분 판정 필터 예: match,near,unknown'); ap.add_argument('--all', action='store_true', help='관련성 없는 행 포함')
ap.add_argument('--type', choices=('monthly', 'jeonse', 'support'), help='임대유형 필터')
ap.add_argument('--target', help='공고 대상 표기 필터 예: 신혼부부 (제목·카테고리 기준, 빈 값=일반/혼합 포함 안 함)')
ap.add_argument('--runs', action='store_true', help='실행 이력'); ap.add_argument('--docs', action='store_true', help='첨부 처리 이력(documents)'); ap.add_argument('--json', action='store_true'); ap.add_argument('--pretty', action='store_true', help='카드형 마크다운(report.py)')
a = ap.parse_args(); con = hdb.connect()
def money(r): return '' if r['deposit'] is None else f"{r['deposit']:g}/{r['rent']:g}"
if a.runs:
    for r in con.execute('SELECT run_id,collected_date,started_at,finished_at,status,max_deposit_manwon,max_rent_manwon,counts_json,errors_json FROM runs ORDER BY run_id DESC LIMIT 20'): print(dict(r))
    raise SystemExit
if a.docs:
    for r in con.execute('SELECT doc_id,source,item_id,kind,status,method,verified,file_name,summary,processed_at FROM documents ORDER BY doc_id DESC LIMIT 50'): print(dict(r))
    raise SystemExit
d = a.date or (con.execute('SELECT MAX(collected_date) FROM listings').fetchone()[0])
w, p = ['collected_date=?'], [d]
if not a.all: w.append('relevant=1')
fits = ['match'] if a.match_only else (a.fit.split(',') if a.fit else None)
fcol = {'monthly': 'fit_monthly', 'jeonse': 'fit_jeonse'}.get(a.type, 'fit')
if a.type == 'support': w.append("lease_type='support'")
elif a.type: w.append(f'{fcol} IS NOT NULL')
if a.target: w.append('targets LIKE ?'); p.append(f'%{a.target}%')
if fits: w.append(f'{fcol} IN (%s)' % ','.join('?' * len(fits))); p += fits
order = "CASE fit WHEN 'match' THEN 0 WHEN 'near' THEN 1 WHEN 'unknown' THEN 2 WHEN 'closed_match' THEN 3 ELSE 4 END, COALESCE(rent_per_m2_won, 1e12)"
rows = [dict(r) for r in con.execute(f'SELECT * FROM listings WHERE {" AND ".join(w)} ORDER BY {order}', p)]
if a.json: print(json.dumps(rows, ensure_ascii=False, indent=1)); raise SystemExit
if a.pretty:
    import report; print(report.render_query(d, rows)); raise SystemExit
print(f'# collected_date={d}  rows={len(rows)}')
print('| 판정(근거) | 단지/공고 | 구·역 | 전용㎡ | 보증금/월세(만원) | 원/㎡ | 접수 | 상태 | N/C/D | 링크 |\n|---|---|---|---|---|---|---|---|---|---|')
for r in rows:
    nm = r['name'] or r['title'][:40]
    print(f"| {r['fit']}({r.get('fit_basis') or '-'}) | {nm} | {r['gu'] or ''} {r['station'] or ''} | {r['area_m2'] or ''} | {money(r)} | {r['rent_per_m2_won'] or ''} | {r['apply_start'] or ''}~{r['apply_end'] or ''} | {r['status']} | {r['is_new']}/{r['is_changed']}/{r['is_due_soon']} | {r['url']} |")
