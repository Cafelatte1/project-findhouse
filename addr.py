#!/usr/bin/env python3
"""주소 추출·정규화·선택 (외부 API·키 없음).
  - extract(text)  : 공고 본문/HTML 텍스트 → notice(라벨 '주택위치·공급위치·위치·소재지·공급주택·주소' 뒤 서울 주소),
                     book(단지별 주소표: '단지명  ··  OO구 OO로 12' 행 → {정규화 단지명: 주소}), partial('(쌍문동 460-50)' 처럼 구 없는 지번)
  - attach(units, text) : 라벨 속 주소 > 주택형 라벨 ↔ 주소표 단지명 매칭 > 공고 단일 주소 → unit['address']
  - pick(best, units, meta, item_addr, gu) : 알림용 확정 주소(사람 meta > 대표 주택형 > 같은 단지명 주택형 > 공고 단일 주소 > 공고 단위 자동 주소)
  - approx(name, gu) : 확정 주소가 없을 때 지도 앱 검색용 '서울 {구} {단지명}' (알림에 '주소(근사)'로 표기)
정규화 형식: '서울 성북구 성북로4길 52' / '서울 마포구 염리동 534' (괄호 지번·건물명·호수는 버림)."""
import re

SEOUL_GU = ('종로구 중구 용산구 성동구 광진구 동대문구 중랑구 성북구 강북구 도봉구 노원구 은평구 서대문구 마포구 양천구 강서구 '
            '구로구 금천구 영등포구 동작구 관악구 서초구 강남구 송파구 강동구').split()
_GU = '(?:' + '|'.join(sorted(SEOUL_GU, key=len, reverse=True)) + ')'
_CITY = r'(?:서울특별시|서울시|서울)'
ROAD = r'[가-힣A-Za-z0-9·]{1,20}?(?:로|길)(?:\s?\d{1,3}(?:번|가)?길)?\s?\d{1,4}(?:-\d{1,4})?(?![\d가-힣])'
JIBUN = r'[가-힣]{1,6}\d{0,2}가?동\s?(?:산\s?)?\d{1,5}(?:-\d{1,5})?(?:번지)?(?![\d])'
FULL = re.compile(rf'(?:{_CITY}\s*)?(?<![가-힣])({_GU})\s*((?:{ROAD})|(?:{JIBUN}))')
ROAD_ONLY = re.compile(rf'(?<![가-힣])({ROAD})')
PARTIAL = re.compile(rf'\(\s*({JIBUN})\s*\)')
LABEL = re.compile(r'(주택\s*위치|공급\s*위치|단지\s*위치|공급\s*주택|위\s*치|소\s*재\s*지|주\s*소)\s*[:：]?')
OFFICE_NAME = re.compile(r'공사|사무소|운영기관|협동조합|본부|지점|(?:복지|지원|주거|입주|상담)\s*센터|접수|우편|이메일')
OFFICE_TAIL = re.compile(r'\d+\s*층(?![가-힣])|빌딩|상가|지점|타워\s*\d')
OFFICE = re.compile(r'공사|센터|운영기관|운영사|사무소|사무실|접수처|장\s*소|우편|서류|이메일|자택|협동조합|관리사무|본사|본부|\(\d{5}\)|전화|문의')

KNOWN_OFFICES = {'서울 강남구 선릉로121길 12',   # LH 서울지역본부(견본주택·현장접수 안내에 '소재지'로 나옴)
                 '서울 강남구 개포로 621'}       # SH 본사(서류 접수처)

def _road_norm(s):
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'(로|길)\s+(\d{1,3}(?:번|가)?길)', r'\1\2', s)          # '구천면로 46길 38' → '구천면로46길 38'
    m = re.match(r'^(.+?(?:로|길))\s?(\d{1,4}(?:-\d{1,4})?)$', s)
    return f'{m.group(1)} {m.group(2)}' if m else s

def _jibun_norm(s):
    s = re.sub(r'번지$', '', re.sub(r'\s+', ' ', s).strip())
    m = re.match(r'^(.+?동)\s?((?:산\s?)?\d.*)$', s)
    return f'{m.group(1)} {m.group(2)}' if m else s

def norm(gu, part):
    """(구, 도로명/지번 부분) → '서울 구 부분'. gu 없으면 '서울 부분'."""
    p = _road_norm(part) if re.search(r'(로|길)\s?\d', part) and not re.search(r'동\d{0,2}가?\s?(산\s?)?\d', part.split()[0] if part.split() else '') else _jibun_norm(part)
    return ' '.join(x for x in ('서울', gu, p) if x)

def find(s):
    """문자열에서 첫 서울 구+주소 → 정규화 주소 또는 None."""
    m = FULL.search(s or '')
    return norm(m.group(1), m.group(2)) if m else None

def nk(s):
    """단지명 정규화 키: 공백·특수문자 제거."""
    return re.sub(r'[\s\-_·.,()\[\]「」<>]', '', s or '')

def extract(text):
    out = dict(notice=[], book={}, partial=[])
    lines = (text or '').splitlines()
    book = []
    for li, ln in enumerate(lines):
        m = FULL.search(ln)
        lab = LABEL.search(ln)
        if m and lab and lab.start() < m.start() and not OFFICE.search(ln[:m.start()]) and not OFFICE.search(ln[m.end():m.end() + 25]) and not OFFICE_TAIL.search(ln[m.end():m.end() + 30]):
            a = norm(m.group(1), m.group(2))
            if a not in out['notice'] and a not in KNOWN_OFFICES: out['notice'].append(a)
            continue
        # 주소표 행: '연번  단지명    OO구 OO로 12   전화 …' (pdftotext -layout 의 2칸 이상 공백 구분)
        mm = m
        gu = m.group(1) if m else None
        if not m:
            mr = re.search(rf'(?<=\s\s)({ROAD})(?=\s|\(|$)', ln)
            if not mr:
                # 줄바꿈된 주소표 행: '44  래미안용두1차      959-1472 …' + 윗줄 '      고산자로29길 18'
                rw = re.match(r'^\s*\d{1,4}\s+(\S.*?)\s{2,}[\d\-~,]{7,}\s', ln)
                up = lines[li - 1] if li else ''
                ma = re.match(rf'^\s{{8,}}(?:({_GU})\s*)?({ROAD})\s*(?:\(.*)?$', up)
                if not (rw and ma): continue
                name = rw.group(1).strip()
                if re.search(r'[가-힣A-Za-z]{2}', name) and len(name) <= 45 and not OFFICE_NAME.search(name):
                    book.append([name, ma.group(1), ma.group(2)])
                continue
            mm, part = mr, mr.group(1)
        else: part = m.group(2)
        segs = [x for x in re.split(r'\s{2,}', ln[:mm.start()].strip()) if x]
        if not segs: continue
        name = re.sub(r'^\d{1,4}\s+', '', segs[-1]).strip()
        if (len(segs) == 1 and re.fullmatch(r'\d{1,4}', segs[0])) or not re.search(r'[가-힣A-Za-z]{2}', name) or len(name) > 45 \
                or ':' in name or '：' in name or LABEL.match(name) or OFFICE_NAME.search(name) or FULL.search(name) or re.search(r'(로|길)\s\d', name):
            continue
        book.append([name, gu, part])
    for i, (n, g, p) in enumerate(book):                  # 구 없는 행: 앞뒤 행의 구가 같으면 채움(주소표는 자치구 순 정렬)
        if g is None:
            prv = next((b[1] for b in reversed(book[:i]) if b[1]), None); nxt = next((b[1] for b in book[i + 1:] if b[1]), None)
            book[i][1] = prv if prv and prv == nxt else None
    if sum(1 for b in book if b[1]) >= 3:                 # 주소표로 인정(우연한 1~2행은 무시)
        for n, g, p in book: out['book'].setdefault(nk(n), norm(g, p))
    for m in PARTIAL.finditer(text or ''):
        a = _jibun_norm(m.group(1))
        if a not in out['partial']: out['partial'].append(a)
    return out

def html_text(h):
    t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', h or '')
    t = re.sub(r'(?i)<br\s*/?>|</(p|div|li|tr|h\d)>', '\n', t)
    t = re.sub(r'(?i)</t[dh]>', '  ', t)
    import html as H
    return H.unescape(re.sub(r'<[^>]+>', ' ', t))

def page_address(h):
    """상세 페이지 HTML → 라벨 붙은 서울 주소(첫 번째) 또는 None."""
    ex = extract(html_text(h))
    return ex['notice'][0] if ex['notice'] else None

_STEM = [r'^\s*\[[^\]]*\]\s*', r'\s*#\d+\s*$', r'\s*\(파일\d+\)\s*$', r'보증금\s*\d+%', r'최대\s*전환', r'\([+\-]\)', r'기본',
         r'\d+(\.\d+)?\s*(㎡|m2|형|타입)', r'\b\d+호\b', r'(청년|신혼부부|공통)\s*$']
def stem(label):
    s = label or ''
    for p in _STEM: s = re.sub(p, ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def match(label, book):
    """주택형 라벨 → 주소표 주소 (정확 키 > 접두 일치 중 가장 긴 키, 3자 이상)."""
    k = nk(stem(label))
    if len(k) < 2 or not book: return None
    if k in book: return book[k]
    best = None
    for bk, a in book.items():
        if len(bk) >= 3 and (k.startswith(bk) or bk.startswith(k) and len(k) >= 3):
            if best is None or len(bk) > len(best[0]): best = (bk, a)
    return best[1] if best else None

def attach(units, text):
    """units 에 address 채움. returns extract 결과(+ single)."""
    ex = extract(text)
    single = ex['notice'][0] if len(ex['notice']) == 1 else None
    if single is None and not ex['notice'] and len(ex['partial']) == 1: single = ex['partial'][0]
    ex['single'] = single
    for u in units:   # 라벨 안의 주소(매입임대 표의 '주소' 열) > 주소표 매칭 > 공고 단일 주소
        u['address'] = find(u.get('unit_label')) or match(u.get('unit_label'), ex['book']) or (single if not ex['book'] else None)
    return ex

def has_gu(a): return bool(re.search(rf'(?<![가-힣]){_GU}(?![가-힣])', a or ''))
def finalize(a, gu=None):
    """구 없는 주소('쌍문동 460-50')에 구·'서울' 접두."""
    if not a: return None
    a = re.sub(rf'^{_CITY}\s*', '', a.strip())
    if has_gu(a): return '서울 ' + a
    return ' '.join(x for x in ('서울', gu if gu in SEOUL_GU else None, a) if x)

def pick(best, units, meta, item_addr=None, gu=None):
    """알림용 확정 주소 또는 None."""
    meta = meta or {}; units = units or []
    if meta.get('address'): return finalize(meta['address'], gu)
    if best and best.get('address'): return finalize(best['address'], gu)
    if best:
        s = nk(stem(best.get('unit_label')))
        for u in units:
            if u.get('address') and s and nk(stem(u.get('unit_label'))) == s: return finalize(u['address'], gu)
    addrs = {u['address'] for u in units if u.get('address')}
    if len(addrs) == 1: return finalize(addrs.pop(), gu)
    if item_addr: return finalize(item_addr, gu)
    return None

_NAME_NOISE = [r'\s*\([^)]*등\)\s*', r'\s+\d+(-\d+)?호.*$', r'\(쉐어\)', r'잔여세대', r'(추가|최초)?\s*모집.*$', r'\s*\([^)]*\)\s*$']
def approx(name, gu=None):
    """단지명만 알 때 지도 검색어: '서울 {구} {단지명}'. 이름이 비면 None."""
    n = name or ''
    for p in _NAME_NOISE: n = re.sub(p, ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    if len(n) < 2: return None
    return ' '.join(x for x in ('서울', gu if gu in SEOUL_GU else None, n) if x)
