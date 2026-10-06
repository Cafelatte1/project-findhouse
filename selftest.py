#!/usr/bin/env python3
"""오프라인 자가점검 (네트워크·개인 설정 불필요). 임시 DB·임시 config 를 쓰므로 실제 housing.db/config.json 을 건드리지 않음.
점검: 판정 로직, 단위 변환, 중복제거, 소스 실패 격리, 같은날 재실행 멱등성, 재게시 승계, 첨부 표 파서,
      판정 근거(basis), LH 구조 변경 감지, documents sha256/parser_version 캐시, 이미지 공고(OCR 끔) 처리, 리포트 템플릿,
      임대유형(월세/전세)·대상(청년/신혼부부) 축: 설정 하위호환, 관련성 동치(구 규칙), 전세 표 파서, 공급대상 열, 전세 판정·🔁 전환,
      혼합 공고의 블록별 판정, 알림 태그, 스키마 마이그레이션 멱등, 채팅 블록 렌더, HUG 공고 파싱, 서울주거포털 주석 제거.
사용: python selftest.py   (실패가 있으면 exit 1)"""
import tempfile, shutil, json, sys
from pathlib import Path
import re
import hdb, sources, collect, docs, units, report, run_daily

# 테스트 전용 고정 기준 (사용자 설정과 무관): 보증금 3000·월세 50만원, 근소초과 +3000/+10
TEST_CFG = {
    'max_deposit_manwon': 3000, 'max_rent_manwon': 50, 'near_tolerance_deposit_manwon': 3000, 'near_tolerance_rent_manwon': 10,
    'exclude_gu': [], 'due_soon_days': 3, 'soco_assumed_window_days': 3, 'http_timeout_sec': 5, 'http_retries': 0,
    'pages': {'sh': 1, 'seoulportal': 1, 'soco': 1, 'lh_lookback_days': 60, 'socialhousing': 1, 'lh_rolling_days': 0},
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

# 12 설정 하위호환: 새 키가 없으면 월세+청년, 태그·지원프로그램 기존과 동일
ok(cfg['lease_types'] == ['monthly'] and cfg['targets'] == ['youth'] and cfg['support_programs'] is False
   and hdb.monthly_tag(cfg) == '@3000/50' and cfg['monthly']['max_deposit_manwon'] == 3000, '구 설정 → 월세+청년·태그 @3000/50·지원프로그램 끔')
cb = hdb.normalize_cfg(dict(TEST_CFG, lease_types=['monthly', 'jeonse'], targets=['youth', 'newlywed']))
ok(cb['support_programs'] is True and cb['jeonse']['max_deposit_manwon'] == 20000 and hdb.monthly_tag(cb) == '@3000/50/newlywed,youth'
   and hdb.jeonse_tag(cb) == '@20000/newlywed,youth', '새 설정 → 전세 기본값·지원프로그램 켬·대상 포함 태그')
co = hdb.normalize_cfg(dict(TEST_CFG), {'max_deposit_manwon': 2500, 'jeonse_max_deposit_manwon': 15000})
ok(co['max_deposit_manwon'] == co['monthly']['max_deposit_manwon'] == 2500 and co['jeonse']['max_deposit_manwon'] == 15000, 'CLI 덮어쓰기: 월세(최상위)·전세 상한')
# 13 관련성: 구 규칙(main 의 EXCLUDE)과 새 규칙(설정 없음)이 대표 제목에서 동일
OLD_EX = re.compile(r'접수결과|접수마감|청약마감|당첨자|예비\d*차|발표|경쟁률|계약안내|계약결과|입주안내|안내문|재계약|서비스|일시중단|일정 연기|신혼|신생아|다자녀|고령자|장기전세|전세주택|든든전세|전세형|희망하우징|기숙사|연극인|예술인|육아|공공한옥|두레주택|가정어린이집')
def old_rel(it):
    if it['source'] == 'soco': return not re.search(r'신혼부부', it['title'])
    if it['source'] == 'socialhousing': return it.get('status') == '모집중' and '서울' in (it.get('address') or '') and not OLD_EX.search(it['title'])
    if it['source'] == 'lh' and it.get('category') in ('행복주택',) and not OLD_EX.search(it['title']): return True
    return bool(collect.RELEVANT.search(it['title'])) and not OLD_EX.search(it['title'])
TITLES = [('sh', '2026년 하반기 신혼·신생아 매입임대주택Ⅰ 입주자 모집공고', None), ('sh', '제51차 장기전세주택 입주자 모집공고', None),
          ('sh', '2026년 2차 청년 매입임대주택 입주자 모집공고', None), ('sh', '청년 매입임대 당첨자 발표', None),
          ('seoulportal', '2026년 전세임대형 든든주택 입주자 모집공고', None), ('lh', '서울대방 신혼희망타운 행복주택', '행복주택'),
          ('lh', '[서울지역본부] 26년 2차 비분양전환형 든든전세주택 입주자 모집공고', '공공임대'), ('lh', '서울 행복주택 예비입주자', '행복주택'),
          ('sh', '2026년 일반주택형 미리내집(공공한옥) 잔여세대 입주자 모집공고', None), ('sh', '고령자복지주택 입주자 모집', None),
          ('soco', '[민간임대] 테스트 청년안심주택 신혼부부 추가모집', None), ('soco', '[민간임대] 테스트 청년안심주택 추가모집', None),
          ('sh', '청년·신혼부부 매입임대주택 입주자 모집', None), ('sh', '재개발임대주택 입주자 모집', None), ('sh', '제8차 장기전세주택2(미리내집) 청약접수 결과 안내', None)]
items = [dict(source=a, item_id=str(i), title=t, category=c) for i, (a, t, c) in enumerate(TITLES)] + \
        [dict(source='socialhousing', item_id='s', title='테스트 사회주택 모집', status='모집중', address='서울 마포구')]
def mixed(it): return collect.notice_targets(it) == {'youth', 'newlywed'}       # 의도적 변경: 혼합 제목은 청년만 골라도 포함
ok(all((old_rel(it) or mixed(it)) == collect.is_relevant(it, cfg) for it in items) and any(mixed(it) and not old_rel(it) for it in items),
   '관련성: 구 설정에서 구 EXCLUDE 규칙과 동일(혼합 대상 제목만 예외로 포함)')
MX = [dict(source='sh', item_id='m1', title='2026년 청년·신혼부부 매입임대주택 입주자 모집공고'), dict(source='sh', item_id='m2', title='청년 및 신혼부부 행복주택 입주자 모집'),
      dict(source='soco', item_id='m3', title='[민간임대] 테스트역 청년안심주택 청년·신혼부부 추가모집'), dict(source='soco', item_id='m4', title='[민간임대] 테스트역 청년안심주택 신혼부부 추가모집'),
      dict(source='sh', item_id='m5', title='2026년 신혼·신생아 매입임대주택 입주자 모집공고'), dict(source='sh', item_id='m6', title='2026년 청년 매입임대주택 입주자 모집공고')]
yo_only, nw_only = dict(targets=['youth']), dict(targets=['newlywed'])
ok([collect.is_relevant(x, yo_only) for x in MX] == [True, True, True, False, False, True]
   and [collect.is_relevant(x, nw_only) for x in MX] == [True, True, True, True, True, False], '혼합 대상 제목: 청년만·신혼만 모두 포함, 단일 대상 제목은 해당 대상만')
CB = dict(lease_types=['monthly', 'jeonse'], targets=['youth', 'newlywed'], support_programs=True)
def rel(prefix): return next(collect.is_relevant(it, CB) for it in items if it['title'].startswith(prefix))
ok(rel('2026년 하반기 신혼') and rel('제51차 장기전세주택') and rel('2026년 전세임대형') and rel('서울대방 신혼희망타운') and rel('2026년 일반주택형')
   and not rel('고령자복지주택') and not rel('제8차 장기전세주택2') and not rel('청년 매입임대 당첨자'), '관련성: 월세+전세·청년+신혼 설정(결과공지·고령자 제외)')
ok(not collect.is_relevant(items[2], dict(targets=['newlywed'])) and collect.is_relevant(items[0], dict(targets=['newlywed'])),
   '관련성: 신혼부부만 → 청년 전용 공고 제외·신혼 공고 포함')
ok([collect.lease_of(dict(title=t)) for t in ('제51차 장기전세주택', '전세임대형 든든주택', '신혼·신생아매입임대주택Ⅱ(전세형)', '행복주택', '전세대 전액 보증 안내')]
   == ['jeonse', 'support', 'jeonse', 'monthly', 'monthly'], '임대유형 분류(제목 기준, 전세대 오탐 없음)')
# 14 전세 표 파서·공급대상 열
TJ = [['주택형', '전용면적', '임대보증금'], ['59A', '59.97', '184,100,000']]
ok(docs.parse_table(TJ) == [], '보증금만 있는 표: 전세 힌트 없으면 무시(월세 오판 방지)')
uj = docs.parse_table(TJ, jeonse=True)
ok(len(uj) == 1 and uj[0]['deposit'] == 18410 and uj[0]['rent'] == 0 and uj[0]['lease_type'] == 'jeonse', '전세 공고 표 → rent=0·lease_type=jeonse')
ok(len(docs.parse_table([['주택형', '전용면적', '전세보증금(원)'], ['59A', '59.97', '184,100,000']])) == 1, '헤더 "전세보증금" → 전세 행')
ok(docs.parse_table([['주택형', '전용면적', '전세대 보증금(원)'], ['59A', '59.97', '184,100,000']]) == [], '"전세대" 는 전세로 보지 않음')
ut = docs.parse_table([['공급대상', '전용면적', '임대보증금', '월임대료'], ['청년', '20.5', '1,000', '30'], ['신혼부부', '40.1', '3,000', '50'],
                       ['고령자', '20.5', '500', '10'], ['청년·신혼부부', '30.5', '2,000', '40']])
ok([u['target'] for u in ut] == ['청년', '신혼부부', '기타', '공통'], f"공급대상 열 → 청년/신혼부부/기타/공통 {[u['target'] for u in ut]}")
# 14b 알림 이름: 단지명/사업명 정리, 여러 단지 사업은 최적 주택형의 단지명
nn = lambda src, t: report.notice_name(dict(source=src, title=t))[:3]
ok(nn('soco', '[민간임대] 동묘앞역 청계로벤하임 추가모집공고') == ('청계로벤하임', None, '동묘앞역')
   and nn('sh', '2026년 재개발임대주택 일반모집 공고(2026. 9. 9.)')[0] == 'SH 재개발임대 일반모집'
   and nn('sh', '[토지임대부 사회주택] 쌍문생활 302호 입주자 모집 공고문')[0] == '쌍문생활'
   and nn('sh', '[청년형] 특화형 매입임대주택(금천구) 입주자 모집 공고(운영기관 : 한지붕 협동조합)')[:2] == ('SH 특화형 매입임대', '금천구')
   and nn('sh', '[청년형] 특화형 매입임대주택 입주자 모집 공고(운영기관 : 한지붕 협동조합)')[0] == 'SH 특화형 매입임대 (한지붕 협동조합)'
   and nn('lh', '2026년 청년 전세임대 1순위 입주자 수시모집')[0] == 'LH 청년 전세임대'
   and nn('sh', '2026년 신정도시마을 잔여세대 입주자모집공고(26. 9. 23.)')[0] == '신정도시마을', '알림 이름: 접두어·날짜·모집공고 제거, 역/구 분리, 사업명은 기관 접두')
ok(report.notice_name(dict(source='sh', title='x 공고', name='사람기록단지'))[0] == '사람기록단지', '알림 이름: item_meta 단지명 우선')
ok(report.unit_place('강동구 서도휴빌(2차) 102동 42C 강동구 성내동 440-26 둔촌동역 O X 보증금50%') == ('서도휴빌(2차)', '강동구', '둔촌동역')
   and report.unit_place('DMC래미안e편한세상(가재울뉴타운3) 최대전환')[0] == 'DMC래미안e편한세상(가재울뉴타운3)'
   and report.unit_place('row1') is None and report.unit_place('29.98㎡') is None and report.unit_place('일반 공급 청년 20A㎡ (910호)') is None,
   '주택형 라벨 → 단지명·구·역 (표 잔해는 무시)')
it = report._item(dict(title='2026년 재개발임대주택 일반모집 공고(2026. 9. 9.)', source='sh', url='u', apply_start=None, apply_end=None, elig_note='세대 기준 공고(무주택세대구성원 등) — 자격 원문 확인',
                       reason_monthly='[자동추출] 동소문한진 562/6.63만원', best_json=json.dumps({'monthly': dict(unit_label='동소문한진', area_m2=32.49, deposit=562, rent=6.63)})),
                  'monthly', cb, show_target=False)
ok(it.split('\n') == ['• 🏠 **[SH 재개발임대 일반모집](u)**', '  - 동소문한진 · 32.49㎡', '  - 보증금 562만 / 월세 6.63만 · 자동추출', '  - 무주택세대 자격'],
   f'알림 항목: 사업명 링크 + 단지명 첫 줄 + 압축 자격, 모르는 접수기간 줄 생략 {it!r}')

# 15 전세 판정
J = lambda l, d, t='공통', r=0, o='human': dict(unit_label=l, area_m2=59, deposit=d, rent=r, target=t, origin=o, lease_type='jeonse' if not r else 'monthly')
ok(hdb.judge_jeonse([J('a', 20000)], {}, False, cb)[0] == 'match', '전세 경계값(=2억) match')
ok(hdb.judge_jeonse([J('a', 20000.1)], {}, False, cb)[0] == 'near' and hdb.judge_jeonse([J('a', 25000.1)], {}, False, cb)[0] == 'no', '전세 근소(+5000) / 범위 밖')
f, r_, b, _ = hdb.judge_jeonse([J('w', 18000, r=5)], {}, False, cb)
ok(f == 'match' and b['conv'] and '전환' in r_, '🔁 월 5만·보증금 1.8억 월세 옵션 → 전세 판정 match(전환)')
ok(hdb.judge_jeonse([J('w', 562, r=6.63)], {}, False, cb) is None, '보증금 5000만 미만 저월세는 전세 아님(재개발임대 등)')
ok(hdb.judge_jeonse([], {}, False, cb, notice_jeonse=True)[0] == 'unknown' and hdb.judge_jeonse([], {}, False, cb) is None, '전세 공고·표 없음 → unknown / 월세 공고 → 해당 없음')
ok(hdb.judge_jeonse([J('n', 15000, '신혼부부')], {}, False, cfg)[0] == 'no' and hdb.judge_jeonse([J('n', 15000, '신혼부부')], {}, False, cb)[0] == 'match',
   '대상 필터: 청년만이면 신혼부부 전세 no, 둘 다면 match')
ok(hdb.judge([J('j', 15000), U('m', 20, 2000, 40)], {}, False, cfg)[2]['unit_label'] == 'm', '월세 판정은 전세 행(rent=0) 무시')
ok(hdb.judge([U('e', 20, 500, 10, '기타')], {}, False, cfg)[0] == 'no', '고령자 등 기타 대상 행은 판정 제외')
# 16 혼합 공고 → 월세·전세 블록 각각 + 지원 프로그램 + HUG(공고 단위)
fake2 = {'sh': lambda c: [dict(source='sh', item_id='3001', title='2026년 청년 매입임대주택 입주자 모집공고', posted='2026-10-01', apply_start='2026-10-05', apply_end='2026-10-08', url='u3')],
         'lh_support': lambda c: [dict(source='lh', item_id='S1', title='신혼·신생아 전세임대 Ⅰ 수시', category='전세임대', posted='2026-03-24', apply_end='2026-12-31', status='접수중', lease='support', url='us')],
         'hug': lambda c: [dict(source='hug', item_id='260929', title='HUG 든든전세주택 12차 입주자 모집 공고', category='든든전세주택', lease='jeonse', posted='2026-09-30',
                                apply_start='2026-09-30', apply_end='2026-10-12', extra_note='서울 420호', elig='공고일 기준 무주택세대구성원', url='uh')]}
sources.ALL = fake2
con = fresh_db('t16'); units.put_unit(con, 'sh', '3001', 'A 기본', 20, 2000, 40, '청년'); units.put_unit(con, 'sh', '3001', 'B 전환', 40, 18000, 5, '신혼부부'); con.commit()
r16 = collect.run(date='2026-10-06'); ok(set(r16['counts']) == {'sh'}, f"구 설정: 전세·지원 소스는 수집 안 함 {r16['counts']}")
L = {x['item_id']: dict(x) for x in con.execute("SELECT * FROM listings WHERE collected_date='2026-10-06'")}
ok(L['3001']['fit'] == L['3001']['fit_monthly'] == 'match' and L['3001']['fit_jeonse'] is None, '구 설정: 판정 = 월세 판정, 전세 판정 없음')
con = fresh_db('t16b'); units.put_unit(con, 'sh', '3001', 'A 기본', 20, 2000, 40, '청년'); units.put_unit(con, 'sh', '3001', 'B 전환', 40, 18000, 5, '신혼부부'); con.commit()
ovr = {'lease_types': ['monthly', 'jeonse'], 'targets': ['youth', 'newlywed']}
r16 = collect.run(ovr, date='2026-10-06'); cb2 = r16['cfg']
L = {x['item_id']: dict(x) for x in con.execute("SELECT l.*, i.notified_fit, i.notified_jeonse, i.notified_support FROM listings l JOIN items i USING(source,item_id) WHERE collected_date='2026-10-06'")}
ok(L['3001']['fit_monthly'] == 'match' and L['3001']['fit_jeonse'] == 'match' and json.loads(L['3001']['best_json'])['jeonse']['conv'], '혼합 공고: 월세 match + 전세(🔁) match')
ok(L['260929']['lease_type'] == 'jeonse' and L['260929']['fit_jeonse'] == 'unknown' and L['260929']['elig_note'] == '공고일 기준 무주택세대구성원', 'HUG: 전세 공고 단위·자격 문구')
ok(L['S1']['fit'] == 'program' and L['S1']['lease_type'] == 'support', '전세임대 → program(가격 판정 없음)')
rows16 = list(L.values())
bm, _ = run_daily.block(con, rows16, 'fit_monthly', 'notified_fit', hdb.monthly_tag(cb2), 'reason_monthly')
bj, _ = run_daily.block(con, rows16, 'fit_jeonse', 'notified_jeonse', hdb.jeonse_tag(cb2), 'reason_jeonse')
sup = [x for x in rows16 if x['fit'] == 'program']
md = report.render_chat(cd='2026-10-06', cfg=cb2, blocks={'monthly': bm, 'jeonse': bj}, support=sup, fails={})
lines = md.split('\n')
ok(lines[0] == '**서울 청년·신혼부부 월세·전세 수집** · 2026-10-06' and lines[1] == '월세 부합 1 / 전세 부합 1 · 공고 1 / 지원 프로그램 1', f'채팅: 제목·한 줄 요약 {lines[:2]}')
ok(md.index('**월세**') < md.index('• 기준: 보증금 ≤3,000만 · 월세 ≤50만') < md.index('**전세**') < md.index('• 기준: 전세 보증금 ≤2억') < md.index('📋 **지원 프로그램**'),
   '채팅: 월세 블록 → 전세 블록 → 지원 프로그램 순서·블록별 기준줄')
ok('🔁 전환 · 보증금 1억 8,000만 / 월세 5만' in md and md.count('✅ **조건 부합** (1)') == 2 and '서울 420호' in md.split('⚠️ **공고 단위 확인** (1)')[1]
   and '· 신혼부부' not in md.split('**전세**')[0] and '· 신혼부부' in md.split('**전세**')[1], '채팅: 🔁 전환은 전세 블록·블록별 ✅·대상 태그=주택형 대상·HUG 공고 단위')
cy = hdb.normalize_cfg(dict(TEST_CFG))
md1 = report.render_chat(cd='2026-10-06', cfg=cy, blocks={'monthly': bm}, support=[], fails={})
ok(md1.split('\n')[0] == '**서울 청년 월세 수집** · 2026-10-06' and '**월세**' not in md1 and '· 청년' not in md1 and md1.split('\n')[1] == '월세 부합 1',
   '채팅: 대상 1개·유형 1개 → 블록 제목·대상 태그 없음(승인 템플릿)')
ok(report.render_chat(cd='d', cfg=cy, blocks={'monthly': {}}) == '**알릴 것 없음**', '채팅: 알릴 것 없음')
items_md = [b_ for b_ in md.split('\n• ')[1:]]
ok(all(sum(1 for x in b_.split('\n') if x.startswith('  - ')) <= 3 for b_ in items_md) and '원문 확인' not in md and '[SH 청년 매입임대](u3)' in md,
   '채팅: 항목당 하위 줄 ≤3, 자리표시 문구 없음, 링크 텍스트 = 짧은 이름')
ok(report.man(18410) == '1억 8,410만' and report.man(2000) == '2,000만' and report.man(20000) == '2억' and report.man(87.5) == '87.5만', '금액 표기(억·만)')
sources.ALL = orig
# 17 스키마 마이그레이션 멱등 (재연결 시 컬럼 중복 추가 없음)
c2 = hdb.connect(); c3 = hdb.connect()
cols = [x[1] for x in c3.execute('PRAGMA table_info(listings)')]
ok(all(cols.count(k) == 1 for k in ('lease_type', 'fit_jeonse', 'best_json', 'elig_note')) and 'notified_jeonse' in [x[1] for x in c3.execute('PRAGMA table_info(items)')], '마이그레이션 멱등·새 컬럼')
# 18 HUG 공고 텍스트 파싱 (마감 표기 상이 → 늦은 날짜 + 경고)
hug_txt = ('HUG 든든전세주택 12차 입주자 모집 공고 [2026.9.30]\n모집공고일은 2026.9.30.(수)이며\n총 900호(서울 420호, 인천 360호)\n'
           '전세보증금은 시중 전세시세의 90% 이하\n(입주자격) 공고일 기준 무주택세대구성원\n신청접수\n9.30(수)\n10:00\n~\n10.12(월)\n17:00\n'
           '구분 신청접수 기간 9.30(수) 10:00 ~ 10.8(목) 17:00\n')
h = sources.parse_hug(hug_txt)
ok(h['posted'] == '2026-09-30' and h['apply_start'] == '2026-09-30' and h['apply_end'] == '2026-10-12' and '서울 420호' in h['extra_note']
   and '90%' in h['extra_note'] and '상이' in h['extra_note'] and h['elig'] == '공고일 기준 무주택세대구성원', f'HUG 공고 파싱 {h}')
# 19 서울주거포털: 셀 안 HTML 주석 제거 + SH 링크 사용
html_sp = ('<table><tr><td>38</td><td>매입임대</td><td>테스트 공고</td><td class="td4"> <!-- 2021-01-25 클래스 수정 -->2026-09-30</td>'
           '<td>2026-11-20</td><td>모집중</td><td>담당</td><td><a href="https://www.i-sh.co.kr/main/lay2/program/S1T294C295/www/brd/m_241/view.do?seq=310650">링크</a></td></tr></table>')
sources.curl = lambda *a, **k: html_sp
try: sp = sources.seoulportal(cfg)
finally: sources.curl = orig_curl
ok(sp and sp[0]['posted'] == '2026-09-30' and sp[0]['url'].endswith('seq=310650'), f"서울주거포털 게시일·링크 {sp and (sp[0]['posted'], sp[0]['url'][-12:])}")

# 20 LH 지역: '서울특별시 외' = 서울+타 지역 → 상세 공급 목록으로 서울 포함 확인, 서울 공급 없는 공고 제외
def lh_page(rows): return "<script>var list = JSON.parse('" + json.dumps(rows, ensure_ascii=False) + "');</script>"
R = lambda cnp, sbd, n: dict(cnpNm=cnp, sbdLgoNm=sbd, totRsdcSplQom=n)
pg_seoul = lh_page([R('서울지역본부 전세임대', '전세(서울강남구)', 25), R('서울지역본부 전세임대', '전세(서울마포구)', 25), R('부산울산지역본부 전세임대', '전세(부산중구)', 10)])
pg_other = lh_page([R('인천지역본부 전세임대', '전세(인천중구)', 30), R('인천지역본부 전세임대', '전세(경기부천시)', 20)])
sp = sources.parse_lh_supply(pg_seoul, ['강남구'])
ok(sp['seoul'] == 50 and sp['seoul_ok'] == 25 and sp['seoul_ok_gu'] == ['마포구'] and sp['total'] == 60, f'LH 상세 공급 목록 → 서울 호수(제외 구 반영) {sp}')
ok(sources.parse_lh_supply('<html>no list</html>') is None, 'LH 상세: 공급 목록 없으면 None')
K = sources.lh_region_keep
ok(K('서울특별시 외', 't', sp) == (True, '서울 25호 · 1개 구') and K('인천광역시 외', 't', sources.parse_lh_supply(pg_other))[0] is False
   and K('서울특별시 외', 't', None)[0] is True and K('인천광역시 외', 't', None)[0] is False and K('경기도', 't', None)[0] is False
   and K('전국', 't', None)[0] is True and K('서울특별시 외', 't', sources.parse_lh_supply(pg_seoul, ['강남구', '마포구']))[0] is False,
   "LH 지역 판정: '서울특별시 외' 유지, 타 지역('인천광역시 외'·단일) 제외, 상세 목록 우선, 서울 공급이 전부 제외 구면 제외")
def lh_row(pan, title, region, status='접수중'):
    return (f'<tr><td>1</td><td>전세임대</td><td><a data-id1="{pan}" data-id2="03" data-id3="13" data-id4="17">{title}</a></td><td>{region}</td>'
            f'<td></td><td>2026.03.24</td><td>2026.12.31</td><td>{status}</td></tr>')
lst = '<table><tr><th>공고명</th></tr>' + lh_row('P1', '청년 전세임대 수시', '서울특별시 외') + lh_row('P2', '인천 신혼 전세임대', '인천광역시 외') + lh_row('P3', '경기 전세임대', '경기도') + '</table>'
def fake_lh(url, data=None, **k):
    if 'selectWrtancList' in url: return lst
    return pg_seoul if 'panId=P1' in data else pg_other
sources.curl = fake_lh
try: sup = sources.lh_support(dict(cfg, exclude_gu=['강남구']))
finally: sources.curl = orig_curl
ok([x['item_id'] for x in sup] == ['P1'] and sup[0]['extra_note'] == '서울 25호 · 1개 구' and sup[0]['lease'] == 'support', f'LH 전세임대: 서울 공급 있는 공고만·호수 비고 {[(x["item_id"], x.get("extra_note")) for x in sup]}')
# 21 주택형 단위 제외 구
ok(hdb.unit_gu(dict(unit_label='강동구 서도휴빌(2차) 102동 42C')) == '강동구' and hdb.unit_gu(dict(unit_label='전세(서울강남구)')) == '강남구'
   and hdb.unit_gu(dict(unit_label='동소문한진')) is None and hdb.unit_gu(dict(unit_label='강남구청역 앞')) is None, '주택형 라벨 → 자치구')
cx = dict(cfg, exclude_gu=['강남구'])
G = lambda l, d, r: dict(unit_label=l, area_m2=30, deposit=d, rent=r, target='공통')
f, r_, b, _ = hdb.judge([G('강남구 A빌 101호', 1000, 20), G('마포구 B빌 201호', 2500, 45), G('C빌', 2900, 49)], {}, False, cx)
ok(f == 'match' and b['unit_label'].startswith('마포구'), f'제외 구 주택형은 판정·대표 선택에서 빠짐 ({f}, {b and b["unit_label"]})')
f, r_, *_ = hdb.judge([G('강남구 A빌', 1000, 20), G('강남구 D빌', 1200, 25)], {}, False, cx)
ok(f == 'excluded_region' and '강남구' in r_, f'모든 주택형이 제외 구 → excluded_region ({f}: {r_})')
ok(hdb.judge([G('강남구 A빌', 1000, 20)], {}, False, cfg)[0] == 'match', '제외 구 설정 없으면 영향 없음')
cxj = dict(cb, exclude_gu=['강남구'])
ok(hdb.judge_jeonse([dict(G('강남구 E', 15000, 0), lease_type='jeonse')], {}, False, cxj)[0] == 'excluded_region'
   and hdb.judge_jeonse([dict(G('강남구 E', 15000, 0), lease_type='jeonse'), dict(G('마포구 F', 19000, 0), lease_type='jeonse')], {}, False, cxj)[2]['unit_label'] == '마포구 F',
   '전세 판정도 주택형 단위 제외 구 적용')

shutil.rmtree(ROOT)
print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'}")
sys.exit(1 if FAILS else 0)
