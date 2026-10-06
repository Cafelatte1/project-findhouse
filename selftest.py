#!/usr/bin/env python3
"""오프라인 자가점검 (네트워크·개인 설정 불필요). 임시 DB·임시 config 를 쓰므로 실제 housing.db/config.json 을 건드리지 않음.
점검: 판정 로직, 단위 변환, 중복제거, 소스 실패 격리, 같은날 재실행 멱등성, 재게시 승계, 첨부 표 파서,
      판정 근거(basis), LH 구조 변경 감지, documents sha256/parser_version 캐시, 이미지 공고(OCR 끔) 처리, 리포트 템플릿.
사용: python selftest.py   (실패가 있으면 exit 1)"""
import tempfile, shutil, json, sys
from pathlib import Path
import hdb, sources, collect, docs, units, report

# 테스트 전용 고정 기준 (사용자 설정과 무관): 보증금 3000·월세 50만원, 근소초과 +3000/+10
TEST_CFG = {
    'max_deposit_manwon': 3000, 'max_rent_manwon': 50, 'near_tolerance_deposit_manwon': 3000, 'near_tolerance_rent_manwon': 10,
    'exclude_gu': [], 'due_soon_days': 3, 'soco_assumed_window_days': 3, 'http_timeout_sec': 5, 'http_retries': 0,
    'pages': {'sh': 1, 'seoulportal': 1, 'soco': 1, 'lh_lookback_days': 60, 'socialhousing': 1},
    'socialhousing_max_age_days': 60,
    'docs': {'enabled': False, 'ocr': False, 'max_items_per_run': 20, 'max_files_per_item': 4, 'ocr_max_pages': 12}}
ROOT = Path(tempfile.mkdtemp(prefix='housing-selftest-'))
(ROOT / 'config.json').write_text(json.dumps(TEST_CFG, ensure_ascii=False)); hdb.CFG = ROOT / 'config.json'
cfg = hdb.load_cfg()
FAILS = []
def ok(c, m):
    print(('PASS ' if c else 'FAIL ') + m)
    if not c: FAILS.append(m)
def fresh_db(name):
    d = ROOT / name; d.mkdir(); hdb.DB = d / 't.db'; docs.DOCS = d / 'docs'; return hdb.connect()
U = lambda l, a, d, r, t='청년': dict(unit_label=l, area_m2=a, deposit=d, rent=r, target=t)

# 1 판정: 보증금비율 옵션 중 하나만 통과해도 match, 최적(원/㎡ 최저) 선택
f, _, b, _ = hdb.judge([U('40%', 20, 2500, 55), U('50%', 20, 2900, 48), U('60%', 20, 3500, 45)], {}, False, cfg)
ok(f == 'match' and b['unit_label'] == '50%', f'옵션조합 match {f} {b and b["unit_label"]}')
f, *_ = hdb.judge([U('a', 20, 3000, 50)], {}, False, cfg); ok(f == 'match', '경계값(=) 포함 match')
f, *_ = hdb.judge([U('a', 20, 3000.1, 50)], {}, False, cfg); ok(f == 'near', '경계 초과 → near')
f, *_ = hdb.judge([U('a', 20, 6700, 74)], {}, False, cfg); ok(f == 'no', '근소초과 범위 밖 → no')
f, *_ = hdb.judge([U('a', 20, 1000, 30)], {'gu': '중구'}, False, dict(cfg, exclude_gu=['중구'])); ok(f == 'excluded_region', '제외 지역(config exclude_gu)')
f, *_ = hdb.judge([U('a', 20, 1000, 30)], {'gu': '중구'}, False, cfg); ok(f == 'match', 'exclude_gu 비어 있으면 제외 없음')
f, *_ = hdb.judge([U('a', 20, 1000, 30)], {}, True, cfg); ok(f == 'closed_match', '마감된 부합 → closed_match')
f, *_ = hdb.judge([U('a', 20, 2900, 48)], {}, False, dict(cfg, max_rent_manwon=45)); ok(f == 'near', 'CLI 덮어쓰기(월세 45) 반영')
f, *_ = hdb.judge([U('a', 40, 1000, 30, '신혼부부')], {}, False, cfg); ok(f == 'no', '신혼부부형만 → no')
# 2 원 단위 변환
ok(units.to_manwon(17463000, True) == 1746.3 and units.to_manwon(333000, True) == 33.3, '원→만원 변환')
# 3 중복제거
items = [dict(source='sh', item_id='1', title='[토지임대부 사회주택] 테스트서원 입주자 모집 공고문', posted='2026-09-07'),
         dict(source='sh', item_id='3', title='[토지임대부 사회주택] 테스트서원 입주자 모집 공고문', posted='2026-09-22'),
         dict(source='seoulportal', item_id='80', title='2026년 테스트마을 잔여세대 입주자모집공고(26. 9. 23.)', posted='2026-09-23', status='모집중'),
         dict(source='sh', item_id='5', title='2026년 테스트마을 잔여세대 입주자모집공고(26. 9. 23.)', posted='2026-09-23')]
collect.dedup(items); ok(items[0].get('dup_of') == 'sh:3' and not items[1].get('dup_of'), 'SH 재게시 최신 1건 대표')
ok(items[2].get('dup_of') == 'sh:5' and items[3].get('status') == '모집중', '서울주거포털↔SH 병합(상태 이관)')
# 4 실패 격리 + 멱등성 (네트워크 소스는 가짜)
T_SH = '[토지지원 사회주택]테스트주택4호 401-3호 입주자 모집 공고'
fake = {'sh': lambda c: [dict(source='sh', item_id='1001', title=T_SH, posted='2026-09-09', url='u')],
        'seoulportal': lambda c: (_ for _ in ()).throw(RuntimeError('timeout')),
        'lh': lambda c: [], 'soco': lambda c: [dict(source='soco', item_id='2001', title='[민간임대] 테스트역 청년안심주택 추가모집공고', posted='2026-10-01', apply_start='2026-10-13', url='u')]}
orig = sources.ALL; sources.ALL = fake
def seed_units(con):
    units.put_unit(con, 'sh', '1001', '401-3 개인실', 18.64, 2000, 37.1, '공통')
    for l, d, r in (('27형 보증금40%', 8640, 59), ('27형 보증금50%', 10800, 49)): units.put_unit(con, 'soco', '2001', l, 21.92, d, r, '청년')
    units.put_meta(con, 'soco', '2001', name='테스트역 청년안심', gu='용산구'); con.commit()
con = fresh_db('t4'); seed_units(con)
r1 = collect.run(date='2026-10-04'); n1 = con.execute('SELECT COUNT(*) FROM listings').fetchone()[0]
r2 = collect.run(date='2026-10-04'); n2 = con.execute('SELECT COUNT(*) FROM listings').fetchone()[0]
ok('seoulportal' in r1['errors'] and r1['counts']['sh'] == 1 and r1['counts']['soco'] == 1, f'소스 1개 실패 시 나머지 진행 {r1["counts"]}')
ok(n1 == n2 == 2, f'같은 날 재실행 멱등 rows {n1}->{n2}')
rows = {x['item_id']: dict(x) for x in con.execute('SELECT * FROM listings')}
ok(rows['1001']['fit'] == 'match' and rows['1001']['is_new'] == 1, f"sh match/new {rows['1001']['fit']}")
ok(rows['2001']['status'] == '접수예정' and rows['2001']['fit'] == 'no', f"soco 접수예정/no {rows['2001']['status']} {rows['2001']['fit']}")
collect.run(date='2026-10-05'); rows = {x['item_id']: dict(x) for x in con.execute("SELECT * FROM listings WHERE collected_date='2026-10-05'")}
ok(rows['1001']['is_new'] == 0 and rows['1001']['is_changed'] == 0, '다음날: 신규/변경 아님')
collect.run(date='2026-10-17'); rows = {x['item_id']: dict(x) for x in con.execute("SELECT * FROM listings WHERE collected_date='2026-10-17'")}
ok(rows['2001']['status'] == '마감' and rows['2001']['fit'] == 'closed', f"soco 가정 접수창 경과 → 마감 {rows['2001']['status']}")
# 5 재게시 승계 (다른 seq, 같은 제목 → 알림상태·판정 승계)
con = fresh_db('t5'); sources.ALL = dict(fake); seed_units(con); collect.run(date='2026-10-04')
tag = f"match@{cfg['max_deposit_manwon']:g}/{cfg['max_rent_manwon']:g}"
con.execute("UPDATE items SET notified_fit=? WHERE item_id='1001'", (tag,)); con.commit()
sources.ALL['sh'] = lambda c: [dict(source='sh', item_id='1999', title=T_SH, posted='2026-10-06', url='u')]
collect.run(date='2026-10-06'); r = dict(con.execute("SELECT l.*,i.notified_fit FROM listings l JOIN items i USING(source,item_id) WHERE collected_date='2026-10-06' AND item_id='1999'").fetchone())
ok(r['fit'] == 'match' and r['is_new'] == 0 and r['is_changed'] == 1 and r['notified_fit'] == tag, f"재게시 승계 fit={r['fit']} new={r['is_new']} chg={r['is_changed']} notified={r['notified_fit']}")
sources.ALL = orig
# 6 첨부 표 파서 (실제 공고 표 구조를 본뜬 픽스처)
t1 = [['공급\n유형', None, '주거\n전용\n(타입)', '금회\n공급\n호수', '(보증금 40%)', None, '(보증금 45%)', None, '보증금 50%', None],
      [None, None, None, None, '(보증금)', '(임대료)', '(보증금)', '(임대료)', '보증금', '임대료'],
      ['청년', '일반', '21.92m2\n(27형)\n414호', '1', '8,640', '59', '9,720', '54', '10,800', '49'],
      ['청년', '일반', '21.92m2\n(27형)\n509호', '1', None, None, None, None, None, None],
      ['소계', None, None, '2', '', None, None, None, None, None],
      ['청년', '일반', '19.55m2\n(24C형)\n819호', '1', '8,480', '57', '9,540', '52', '10,600', '47'],
      ['합계', None, None, '3', '', None, None, None, None, None]]
us = docs._dedup_units(docs.parse_table(t1))
ok(len(us) == 6 and {(u['area_m2'], u['deposit'], u['rent']) for u in us} >= {(21.92, 8640, 59), (19.55, 10600, 47)} and all(u['target'] == '청년' for u in us),
   f'표파서: 보증금비율 3옵션·병합셀 승계·만원 단위 ({len(us)}행)')
ok(all(u['supply'] == 1 for u in docs.parse_table(t1)), '표파서: 공급호수 합계 검산 통과 시 저장')
t2 = [['단지', '신청\n유형\n(전용)', '기준 임대 조건', None, '전환\n구분', '최대 전환 시 임대 조건', None],
      [None, None, '임대보증금\n(천원)', '월임대료\n(원)', None, '임대보증금(천원)', '월 임대료(원)'],
      ['테스트마을', '39㎡', '44,000', '335,000', '(+)', '80,000', '134,000'],
      [None, None, None, None, '(-)', '17,600', '390,000']]
vals = {(u['deposit'], u['rent']) for u in docs.parse_table(t2)}
ok(vals == {(4400, 33.5), (8000, 13.4), (1760, 39)}, f'표파서: 천원/원 혼합 단위 + 전환 옵션 {sorted(vals)}')
t3 = [['공급\n유형', None, '주거\n전용\n(타입)', '금회\n공급\n호수', '보증금', '임대료', '입주예정일'],
      ['청년', '일반', '16.39m\n(A타입)', '804', '117,000,000', '307,800', '2026,12,16'],
      ['청년', '일반', '16.39m\n(A타입)', '705', '110,000,000', '322,000', '2026,12,07'], ['합계', None, None, '2', '', None, None]]
us = docs.parse_table(t3)
ok([(u['area_m2'], u['deposit'], u['rent'], u['supply']) for u in us] == [(16.39, 11700, 30.78, None), (16.39, 11000, 32.2, None)], '표파서: 원 단위·호수열이 호실번호면 공급수 버림')
rooms = docs._room_area_map([[['층', '호수', '평형', '입주대상', '전용면적'], [None, '202', '15.0평', '1~2인', '35.16']]])
us = docs.parse_table([['층', '호수', '평형', '', None], [None, None, None, '보증금 비\n보증금 (원)', '율 40%\n월 임대료'], [None, '202', '15.0평', '121,682,000', '(원)\n607,000']], '', '', rooms)
ok(us and us[0]['area_m2'] == 35.16 and us[0]['deposit'] == 12168.2 and us[0]['rent'] == 60.7 and '40%' in us[0]['unit_label'], '표파서: 다른 표의 호실→면적 조인')
ok(docs.to_manwon(32400, None, 'rent', None) == 3.24 and docs.to_manwon(9700, None, 'dep', None) == 9700 and docs.to_manwon(44000, 'k', 'dep', None) == 4400, '단위 추정(원/만원/천원)')
ok(docs.parse_table([['구분', '내용'], ['보증금', '임대료 설명 텍스트']]) == [], '서술형 표는 무시')
# 7 판정 근거(basis): human 우선, auto 는 판정+표기, ocr 은 unverified
A = lambda l, d, r, o: dict(unit_label=l, area_m2=20, deposit=d, rent=r, target='청년', origin=o)
f, rs, b, bs = hdb.judge([A('h', 6000, 80, 'human'), A('a', 1000, 30, 'auto')], {}, False, cfg); ok(f == 'no' and bs == 'human', f'human 값이 있으면 auto 무시 ({f},{bs})')
f, rs, b, bs = hdb.judge([A('a', 1000, 30, 'auto')], {}, False, cfg); ok(f == 'match' and bs == 'auto' and rs.startswith('[자동추출]'), 'auto 단독 → match + [자동추출] 표기')
f, rs, b, bs = hdb.judge([A('o', 1000, 30, 'ocr')], {}, False, cfg); ok(f == 'unverified' and '잠정 match' in rs, 'ocr 단독 → unverified(잠정 match)')
f, rs, b, bs = hdb.judge([A('o', 1000, 30, 'ocr')], {}, True, cfg); ok(f == 'closed', 'ocr + 마감 → closed')
# 8 LH 구조 변경 감지 (curl 모의)
orig_curl = sources.curl
sources.curl = lambda *a, **k: '<table><tr><th>공고명</th></tr><tr>' + '<td>x</td>' * 9 + '</tr></table>'
try: sources.lh(cfg); ok(False, 'LH 행 있는데 파싱 0건 → 예외')
except RuntimeError as e: ok('구조 변경' in str(e), 'LH 행 있는데 파싱 0건 → 예외')
sources.curl = lambda *a, **k: '<table><tr><th>공고명</th></tr></table>'
try: sources.lh(cfg); ok(False, 'LH 서울·전국 0건 → 예외')
except RuntimeError as e: ok('전국' in str(e), 'LH 서울·전국 모두 0건 → 장애로 보고')
calls = []
def fake_curl(url, data=None, **k):
    calls.append(data); return '<tr><th>공고명</th></tr>' + ('' if 'cnpCd=11' in (data or '') else '<tr><td data-id1="1"></td></tr>')
sources.curl = fake_curl; r = sources.lh(cfg); ok(r == [] and len(calls) == 2, 'LH 서울 0건·전국 >0 → 정상 0건')
sources.curl = orig_curl
# 9 documents: sha256 + parser_version 캐시 (다운로드·추출 모의, 픽스처 파일 불필요)
n_extract = [0]
def fake_extract(path, page_outdir=None):
    n_extract[0] += 1
    us = [dict(unit_label=f'27형 보증금{p}%', target='청년', area_m2=21.92, deposit=d, rent=r, supply=None, page=3, table_index=0) for p, d, r in ((40, 8640, 59), (50, 10800, 49))]
    info = dict(pages=10, tables=1)
    return us, info, docs.build_structured(kind='pdf-text', method='pdfplumber', units=us, info=info, text='', page_images={3: str(path) + '_p3.png'})
o_curl, o_list, o_cls, o_ext = docs._curl, docs.list_attachments, docs.classify, docs.extract_pdf_text
docs._curl = lambda url, out=None, referer=None, timeout=60: Path(out).write_bytes(b'%PDF-1.4 same-bytes')
docs.list_attachments = lambda s, i, u: [dict(name='공고문.pdf', url='u1' + i, referer=None)]
docs.classify = lambda p, n: 'pdf-text'; docs.extract_pdf_text = fake_extract
con = fresh_db('t9')
docs.process_item(con, 'soco', 'A', 'x', cfg); docs.process_item(con, 'soco', 'B', 'x', cfg); r3 = docs.process_item(con, 'soco', 'A', 'x', cfg)
st = [x[0] for x in con.execute('SELECT status FROM documents ORDER BY doc_id')]
nB = con.execute("SELECT COUNT(*) FROM units WHERE item_id='B' AND origin='auto'").fetchone()[0]
ok(st == ['ok', 'dup_hash'] and nB == 2 and r3.get('skipped') and n_extract[0] == 1, f'documents: 같은 해시 재파싱 안 함·결과 복사 {st} units={nB} extract={n_extract[0]}')
doc = dict(con.execute("SELECT parser_version, structured_json FROM documents WHERE item_id='A'").fetchone()); sj = json.loads(doc['structured_json'] or '{}')
ok(doc['parser_version'] == docs.PARSER_VERSION and sj.get('units') and sj['units'][0].get('page') == 3, '구조화 JSON + parser_version + 페이지 참조 저장')
pv = docs.PARSER_VERSION; docs.PARSER_VERSION = pv + '-test'
r4 = docs.process_item(con, 'soco', 'A', 'x', cfg); docs.PARSER_VERSION = pv
ok(not r4.get('skipped') and n_extract[0] == 2, f'parser_version 변경 시 재처리 extract={n_extract[0]}')
# 10 이미지형 공고 + OCR 끔(기본) → needs_agent, 숫자 없음 → 판정 unknown(가격 미확인)
docs.classify = lambda p, n: 'pdf-image'
con = fresh_db('t10'); r = docs.process_item(con, 'sh', 'IMG', 'x', cfg)
d = dict(con.execute("SELECT status, method FROM documents WHERE item_id='IMG'").fetchone())
n = con.execute("SELECT COUNT(*) FROM units WHERE item_id='IMG'").fetchone()[0]
ok(d['status'] == 'needs_agent' and n == 0, f'이미지 공고(OCR 끔) → needs_agent, units 0 ({d})')
f, *_ = hdb.judge([], {}, False, cfg); ok(f == 'unknown', '가격 미확인 → unknown (에이전트 확인 대상)')
docs._curl, docs.list_attachments, docs.classify, docs.extract_pdf_text = o_curl, o_list, o_cls, o_ext
# 11 report 템플릿
card = report.render_listing_card(dict(name='테스트단지', title='t', gu='마포구', station='합정', area_m2=20, deposit=1000, rent=30,
    rent_per_m2_won=15000, apply_start='2026-10-01', apply_end='2026-10-05', status='모집중', fit='match', fit_basis='auto',
    fit_reason='[자동추출] 보증금40% 1000/30만원', url='https://example.com'))
ok('테스트단지' in card and '마포구' in card and '자동추출' in card, 'report 카드 렌더')
md = report.render_alert(cd='2026-10-05', cfg=cfg, res=dict(counts={'sh': 1}, elapsed_sec=1.2, source_sec={'sh': 1.0}, docs=[]),
                         sections=[('match', [dict(name='A', title='A', gu='중구', station='', area_m2=18, deposit=2000, rent=37,
                            rent_per_m2_won=20000, apply_start='?', apply_end='상시', status='모집중', fit='match', fit_basis='human',
                            fit_reason='2000/37만원', url='u')])])
ok('부합' in md and f"{cfg['max_deposit_manwon']:g}만원" in md and '알릴 것 없음' not in md, 'report 알림 헤더·섹션')
ok(docs._rhwp() is not None, 'rhwp-python 로드(HWP 파서; 실패 시 hwp5html 폴백만 사용)')

shutil.rmtree(ROOT)
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
sys.exit(1 if FAILS else 0)
