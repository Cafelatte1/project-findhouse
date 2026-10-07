#!/usr/bin/env python3
"""공고 첨부 처리: 첨부 목록 → 다운로드(sha256) → 형식 판별 → 추출 → units(origin=auto|ocr, verified=0) 저장.
  텍스트 PDF : pdfplumber 표 추출 (임대료 표 페이지만)          → origin=auto
  HWP / HWPX : rhwp-python 표 IR (폴백: hwp5html / section XML) → 같은 표 파서 → origin=auto
  이미지 PDF/이미지 : 기본(config docs.ocr=false) → 페이지 PNG 만 남기고 status=needs_agent (에이전트가 직접 읽음)
                     docs.ocr=true 또는 --ocr → pdftoppm + tesseract(kor) 숫자 후보 → origin=ocr (판정 'unverified')
사용: python docs.py process <source> <item_id> [--force] [--ocr]   |  python docs.py list [source item_id]"""
import re, json, hashlib, subprocess, datetime as dt, html as H, zipfile, sys, tempfile, shutil
from pathlib import Path
import hdb, addr

DOCS = hdb.BASE / 'docs'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36'
SUPPORTED = ('sh', 'soco', 'socialhousing')
FORM = re.compile(r'신청서|서약서|동의서|양식|서식|체크리스트|별지|위임장|확인서|제출서류|평면도|배치도')
MONEY_COLS_BAD = re.compile(r'계약금|잔금|중도금|관리비')
JEONSE_HDR = re.compile(r'전세(?!대)')          # 표 헤더/제목의 전세 신호 ('전세대'=전 세대 제외). 본문 키워드는 쓰지 않음
TARGET_COL = re.compile(r'공급\s*대상|입주\s*대상|모집\s*대상|공급\s*유형|계층')
OTHER_TARGET = re.compile(r'고령|주거급여|수급자')
PARSER_VERSION = '2.2'   # 2.2: 주소(단지별 주소표·주택위치) / 2.1: 전세(보증금 단독) 표·공급대상 열·고령자 행 분리   # 파서/구조화 스키마 버전 — 바뀌면 sha256 캐시 무효화
# FreeType: rhwp-python 번들 libfreetype 이 FT_Palette_Data_Get 심볼이 비어 시스템 lib 를 먼저 로드
def _preload_freetype():
    import ctypes
    import ctypes.util, os, glob
    cands = [os.environ.get('FREETYPE_LIB'), *glob.glob('/usr/lib/*-linux-gnu/libfreetype.so.6'), '/usr/lib/libfreetype.so.6',
             '/usr/lib64/libfreetype.so.6', ctypes.util.find_library('freetype')]
    for p in [c for c in cands if c]:
        try: ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL); return True
        except OSError: pass
    return False
_RHWP = None
def _rhwp():
    """rhwp-python (import rhwp). 실패 시 None — hwp5html 폴백."""
    global _RHWP
    if _RHWP is False: return None
    if _RHWP is not None: return _RHWP
    try:
        _preload_freetype(); import rhwp as m; _RHWP = m; return m
    except Exception as e:
        _RHWP = False; return None

def _curl(url, out=None, referer=None, timeout=60):
    a = ['curl', '-s', '-L', '--fail', '-m', str(timeout), '-A', UA] + (['-e', referer] if referer else []) + ([ '-o', str(out)] if out else []) + [url]
    r = subprocess.run(a, capture_output=True)
    if r.returncode: raise RuntimeError(f'curl rc={r.returncode} {url[:90]}')
    return None if out else r.stdout.decode('utf-8', 'ignore')

# ---------------------------------------------------------------- 첨부 목록
_LAST_PAGE = {}   # url → 상세 페이지 HTML (list_attachments 가 받은 것을 process_item 이 주소 추출에 재사용)
def list_attachments(src, iid, url):
    if src == 'sh':
        s = _curl(url); _LAST_PAGE[url] = s; m = re.search(r'initParam\.downList = (\[.*?\]);', s)
        return [dict(name=f['oriFileNm'], referer=url,
                     url=f"https://www.i-sh.co.kr/app/com/file/innoFD.do?brdId={f['brdId']}&seq={f['seq']}&fileTp={f['fileTp']}&fileSeq={f['fileSeq']}")
                for f in (json.loads(m.group(1)) if m else [])]
    if src == 'soco':
        s = _curl(url); _LAST_PAGE[url] = s; out = {}
        for m in re.finditer(r'href="(/coHouse/cmmn/file/fileDown\.do\?atchFileId=\w+&(?:amp;)?fileSn=\d+)"[^>]*>\s*([^<]+?)\s*</a>', s):
            out.setdefault(H.unescape(m.group(1)), H.unescape(m.group(2)))
        return [dict(name=n, url='https://soco.seoul.go.kr' + u, referer=url) for u, n in out.items()]
    if src == 'socialhousing':
        s = _curl(url); _LAST_PAGE[url] = s; out = {}
        for m in re.finditer(r'href="(/index\.php\?module=file&(?:amp;)?act=procFileDownload[^"]+)"[^>]*>\s*([^<]+?)\s*</a>', s):
            out.setdefault(H.unescape(m.group(1)), H.unescape(m.group(2)))
        return [dict(name=n, url='https://socialhousing.kr' + u, referer=url) for u, n in out.items()]
    return []

def classify(path, name):
    b = path.read_bytes()[:8]; ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if b.startswith(b'%PDF'):
        r = subprocess.run(['pdftotext', '-l', '6', str(path), '-'], capture_output=True)
        txt = r.stdout.decode('utf-8', 'ignore')
        return 'pdf-text' if len(re.sub(r'\s', '', txt)) > 300 else 'pdf-image'
    if b.startswith(b'\xd0\xcf\x11\xe0'): return 'hwp' if ext in ('hwp', '') else 'other'
    if b.startswith(b'PK'): return 'hwpx' if ext == 'hwpx' else ('zip' if ext == 'zip' else 'other')
    if b[:3] == b'\xff\xd8\xff' or b.startswith(b'\x89PNG'): return 'image'
    return 'other'

# ---------------------------------------------------------------- 숫자/단위
def num(cell):
    if cell is None: return None
    t = re.sub(r'\([^)]*\)', '\n', str(cell)).replace('원', '').replace(' ', '')
    for l in t.split('\n'):
        if re.fullmatch(r'\d{1,3}(,\d{3})+|\d+(\.\d+)?', l): return float(l.replace(',', ''))
    return None
def col_unit(label):
    if '천원' in label: return 'k'
    if '만원' in label: return 'm'
    if re.search(r'\(\s*원\s*\)|[^천만]원\s*\)|\b원$', label): return 'w'
    return None
def to_manwon(v, unit, kind, page_hint):
    if v is None: return None
    if unit is None:
        if kind == 'dep': unit = 'w' if v >= 1e6 else ('k' if page_hint == 'k' and v >= 1e4 else 'm')
        else: unit = 'w' if v >= 1000 else ('k' if page_hint == 'k' else 'm')
    return round(v / {'w': 1e4, 'k': 10, 'm': 1}[unit], 4)
def page_unit(text):
    m = re.search(r'단위\s*[:：]?\s*(천원|만원|원)', text or '')
    return {'천원': 'k', '만원': 'm', '원': 'w'}[m.group(1)] if m else None
def area_of(cell):
    if not cell: return None
    s = str(cell)
    m = re.search(r'(\d{1,3}(?:\.\d+)?)\s*[A-Za-z]?\s*(?:㎡|m2|m²|㎥|m\b)', s) or re.search(r'(?<![\d.])(\d{1,3}\.\d+)(?![\d.])', s)
    v = float(m.group(1)) if m else None
    return v if v and 5 <= v <= 200 else None

# ---------------------------------------------------------------- 표 → 주택형
def parse_table(rows, page_text='', heading='', area_by_room=None, jeonse=False):
    """임대료 표 → units. jeonse=True(전세 공고) 이거나 헤더에 '전세'가 있으면 월세 열 없는 보증금 단독 표도 rent=0, lease_type=jeonse 로."""
    rows = [[(c if c is None else str(c)) for c in r] for r in rows if r]
    if len(rows) < 2: return []
    ncol = max(len(r) for r in rows); rows = [r + [None] * (ncol - len(r)) for r in rows]
    first = next((i for i, r in enumerate(rows) if sum(num(c) is not None for c in r) >= 2), None)
    if not first: return []
    hdr = [list(r) for r in rows[:first]]
    for r in hdr[:-1]:                         # 상단 헤더행만 가로 병합 채움 (마지막 헤더행은 그대로)
        for j in range(1, ncol):
            if r[j] is None: r[j] = r[j - 1]
    labels = [re.sub(r'\s+', ' ', ' '.join((h[j] or '') for h in hdr)).strip() for j in range(ncol)]
    last = [re.sub(r'\s+', ' ', (hdr[-1][j] or '')) for j in range(ncol)]
    kind = {}
    for j, lab in enumerate(labels):
        if MONEY_COLS_BAD.search(lab): continue
        prim = last[j] or lab
        if re.search(r'임대료|월세|사용료', prim): kind[j] = 'rent'
        elif '보증금' in prim or re.search(r'전세금', prim): kind[j] = 'dep'
        elif re.search(r'임대료|월세|사용료', lab): kind[j] = 'rent'
        elif '보증금' in lab or re.search(r'전세금', lab): kind[j] = 'dep'
    deps = [j for j in kind if kind[j] == 'dep']; rents = [j for j in kind if kind[j] == 'rent']
    if not deps: return []
    if not rents:                                # 보증금 단독 표 = 전세 표일 때만 인정 (계약금 표 등은 위에서 제외)
        if not (jeonse or JEONSE_HDR.search(' '.join(labels) + ' ' + (heading or ''))): return []
        pairs = [(deps[0], None)]
    else:
        pairs, used = [], set()
        for d in deps:
            r = next((r for r in rents if r > d and r not in used), None)
            if r is not None: used.add(r); pairs.append((d, r))
    area_col = next((j for j, l in enumerate(labels) if '전용' in l and '평' not in l and '공용' not in l and j not in kind), None)
    room_col = next((j for j, l in enumerate(labels) if re.search(r'호수|호실|공실', l) and j not in kind and j != area_col), None)
    sup_col = next((j for j, l in enumerate(labels) if re.search(r'공급\s*호수|모집\s*호수|세대수|공급\s*세대|금회', l) and j not in kind and j != area_col), None)
    tgt_col = next((j for j, l in enumerate(labels) if j >= 2 and TARGET_COL.search(l) and j not in kind and j not in (area_col, sup_col)), None)
    hint = page_unit(page_text)
    out, prev, sup_vals, total = [], None, [], None
    for r in rows[first:]:
        txt = ' '.join(c for c in r if c)
        if re.search(r'소\s*계|합\s*계|총\s*계|^계\b|입주가능일', txt.strip()):
            if re.search(r'합\s*계|총\s*계', txt) and sup_col is not None and num(r[sup_col]) is not None: total = num(r[sup_col])
            continue
        if prev is not None: r = [prev[j] if r[j] is None else r[j] for j in range(ncol)]   # 세로 병합 셀 승계
        prev = r
        tcells = ' '.join((r[j] or '') for j in range(min(2, ncol))) + (' ' + (r[tgt_col] or '') if tgt_col is not None else '')
        if '신혼' in tcells and '청년' in tcells: target = '공통'            # '청년 또는 신혼부부' 행
        elif '신혼' in tcells: target = '신혼부부'
        elif '청년' in tcells: target = '청년'
        elif OTHER_TARGET.search(tcells): target = '기타'                    # 고령자·주거급여 등 — 판정 대상 아님
        else: target = '공통'
        area = area_of(r[area_col]) if area_col is not None else None
        if area is None and area_by_room and room_col is not None:
            area = area_by_room.get(re.sub(r'\D', '', r[room_col] or ''))
        sup = num(r[sup_col]) if sup_col is not None else None
        sup_vals.append(sup)
        lab_parts = []
        for j in range(ncol):
            if j in kind or j == sup_col or not r[j]: continue
            c = re.sub(r'\s+', ' ', r[j]).strip()
            if len(c) > 30 or re.search(r'20\d\d[.,\-년]', c) or num(c) is not None and j != room_col: continue
            lab_parts.append(c)
        base = ' '.join(dict.fromkeys(lab_parts))[:60]
        for d, rc in pairs:
            dv = num(r[d]); rv = num(r[rc]) if rc is not None else 0
            if dv is None or rv is None: continue
            dep = to_manwon(dv, col_unit(labels[d]), 'dep', hint)
            rent = to_manwon(rv, col_unit(labels[rc]), 'rent', hint) if rc is not None else 0
            if not (50 <= dep <= 200000 and (rc is None or 0.3 <= rent <= 600)): continue
            opt = re.search(r'(\d{2,3})\s*%', labels[d] + ' ' + (labels[rc] if rc is not None else ''))
            conv = '최대전환' if '전환' in labels[d] else ''
            if not base and area: base = f'{area:g}㎡'
            lbl = ' '.join(x for x in ((f'[{heading}]' if heading else ''), base, (f'보증금{opt.group(1)}%' if opt else ''), conv) if x)
            opt_s = (f'보증금{opt.group(1)}%' if opt else '') + ((' ' + conv) if conv else '')
            out.append(dict(unit_label=lbl or f'row{len(out)+1}', target=target, area_m2=area, deposit=dep, rent=rent, supply=sup,
                            option=opt_s.strip() or None, lease_type='jeonse' if rc is None else 'monthly',
                            note=(f"원표 {r[d]!s}/{r[rc]!s}" if rc is not None else f"원표 전세 {r[d]!s}").replace('\n', ' ')))
    ok_sup = total is not None and sup_vals and all(v is not None for v in sup_vals) and abs(sum(sup_vals) - total) < 0.5
    for u in out:
        if not ok_sup: u['supply'] = None
        elif u['supply'] is not None: u['supply'] = int(u['supply'])
    return out

def _dedup_units(us):
    seen, out = set(), []
    for u in us:
        k = (u['target'], u['area_m2'], u['deposit'], u['rent'])
        if k in seen: continue
        seen.add(k); base = u['unit_label']; i = 2
        while any(x['unit_label'] == u['unit_label'] for x in out): u['unit_label'] = f'{base} #{i}'; i += 1
        out.append(u)
    return out

def _room_area_map(tables):
    m = {}
    for rows in tables:
        if len(rows) < 2: continue
        hdr = ' '.join(str(c) for c in rows[0] if c)
        try:
            rc = next(j for j, c in enumerate(rows[0]) if c and re.search(r'호수|호실|공실', c))
            ac = next(j for j, c in enumerate(rows[0]) if c and '전용' in c)
        except StopIteration: continue
        for r in rows[1:]:
            if len(r) > max(rc, ac) and r[rc] and area_of(r[ac]): m[re.sub(r'\D', '', r[rc])] = area_of(r[ac])
    return m

# ---------------------------------------------------------------- 추출기
def extract_pdf_text(path, page_outdir=None, jeonse=False):
    """텍스트 PDF: pdfplumber 표 + 해당 페이지 PNG 렌더 + 구조화용 page/bbox 필드."""
    import pdfplumber
    full = subprocess.run(['pdftotext', '-layout', str(path), '-'], capture_output=True).stdout.decode('utf-8', 'ignore').split('\f')
    rx = r'임대료|월세|사용료|전세(?!대)' if jeonse else r'임대료|월세|사용료'   # 전세 공고일 때만 전세 표 페이지 추가 선택
    pages = [i for i, t in enumerate(full) if '보증금' in t and re.search(rx, t)][:12]
    tabs = []
    with pdfplumber.open(str(path)) as pdf:
        for i in pages:
            if i >= len(pdf.pages): continue
            p = pdf.pages[i]
            for ti, t in enumerate(p.find_tables()):
                x0, top, x1, bot = t.bbox
                head = (p.crop((max(0, x0), max(0, top - 45), min(p.width, x1), top)).extract_text() or '') if top > 1 else ''
                h = '특별공급' if '특별공급' in head else ('일반공급' if '일반공급' in head else ('우선공급' if '우선공급' in head else ''))
                tabs.append((t.extract(), full[i], h, i + 1, ti, [round(x0,1), round(top,1), round(x1,1), round(bot,1)]))
    rmap = _room_area_map([t[0] for t in tabs]); us = []
    for rows, txt, h, pg, ti, bbox in tabs:
        for u in parse_table(rows, txt, h, rmap, jeonse=jeonse):
            u['page'] = pg; u['table_index'] = ti; u['bbox'] = bbox; us.append(u)
    _fill_single_area(us, [t[0] for t in tabs])
    us = _dedup_units(us)
    ex = addr.attach(us, '\n'.join(full))                 # 주택형 주소: 단지별 주소표 매칭 > 공고 단일 주소
    page_images = _render_pdf_pages(path, sorted({u['page'] for u in us if u.get('page')}), page_outdir) if page_outdir else {}
    info = dict(pages=sorted({p + 1 for p in pages}), tables=len(tabs), page_images=page_images)
    structured = build_structured(kind='pdf-text', method='pdfplumber', units=us, info=info, text='\n'.join(full[:8]), page_images=page_images, addresses=ex)
    return us, info, structured


def _guess_meta(text):
    """공고 본문에서 단지명·주소·접수기간·자격 휴리스틱(없으면 None). 숫자는 원문에 있을 때만."""
    t = text or ''
    meta = dict(name=None, address=None, apply_start=None, apply_end=None, eligibility=None)
    ex = addr.extract(t)                                   # 라벨(주택위치·소재지 등) 붙은 서울 주소가 하나일 때만
    meta['address'] = ex['notice'][0] if len(ex['notice']) == 1 else (ex['partial'][0] if not ex['notice'] and len(ex['partial']) == 1 else None)
    m = re.search(r'(?:단지\s*명|주택\s*명|사업\s*명)\s*[:：]?\s*([^\n]{2,40})', t)
    if m: meta['name'] = re.sub(r'\s+', ' ', m.group(1)).strip()[:60]
    # 접수 기간: 2026. 9. 29. ~ 10. 2. / 2026-10-06 ~ 2026-10-08 등
    m = re.search(r'(?:인터넷\s*)?(?:신청|청약|접수)\s*(?:기간|일정)?\s*[:：]?\s*(20\d\d)\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*[.일]?\s*[~\-～]\s*(?:(20\d\d)\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})', t)
    if m:
        y1, mo1, d1, y2, mo2, d2 = m.groups(); y2 = y2 or y1
        meta['apply_start'] = f'{y1}-{int(mo1):02d}-{int(d1):02d}'; meta['apply_end'] = f'{y2}-{int(mo2):02d}-{int(d2):02d}'
    m = re.search(r'(무주택[^\n]{0,40}소득[^\n]{0,40}%[^\n]{0,20})', t)
    if m: meta['eligibility'] = re.sub(r'\s+', ' ', m.group(1)).strip()[:120]
    return meta

def _render_pdf_pages(path, pages_1based, outdir, dpi=120):
    """임대료 표가 있는 페이지만 PNG 로 렌더. returns {page_no: path}"""
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    out = {}
    for pg in pages_1based:
        dest = outdir / f'p{pg:03d}.png'
        if not dest.exists():
            subprocess.run(['pdftoppm', '-r', str(dpi), '-png', '-f', str(pg), '-l', str(pg), '-singlefile',
                            str(path), str(dest.with_suffix(''))], capture_output=True, timeout=120)
        if dest.exists(): out[pg] = str(dest)
    return out

def build_structured(*, kind, method, units, info, text='', page_images=None, tables_meta=None, addresses=None):
    """에이전트 공유용 구조화 JSON. addresses = addr.attach() 결과(전체 본문 기준) — 있으면 meta.address 를 그것으로."""
    meta = _guess_meta(text)
    if addresses is not None:
        meta['address'] = addresses.get('single'); meta['address_book'] = len(addresses.get('book') or {})
    us = []
    for u in units:
        us.append(dict(unit_label=u.get('unit_label'), target=u.get('target'), area_m2=u.get('area_m2'),
                       deposit=u.get('deposit'), rent=u.get('rent'), supply=u.get('supply'), lease_type=u.get('lease_type'),
                       option=u.get('option'), page=u.get('page'), table_index=u.get('table_index'),
                       bbox=u.get('bbox'), page_image=(page_images or {}).get(u.get('page')),
                       note=u.get('note'), address=u.get('address')))
    return dict(parser_version=PARSER_VERSION, kind=kind, method=method, meta=meta, units=us,
                pages_rendered=sorted((page_images or {}).values()),
                crops=info.get('crops') or [], tables=info.get('tables'),
                pages=info.get('pages'), text_excerpt=(text or '')[:1500],
                sample=info.get('sample') or [])

def _fill_single_area(us, tables):
    """임대료 표에 면적이 없고, 같은 페이지들에 '전용면적' 단일값 표가 하나뿐이면 그 값을 채움(예: 쌍문생활 302호)."""
    if not us or any(u['area_m2'] for u in us): return
    vals = set()
    for rows in tables:
        for i in range(len(rows) - 1):
            for j, c in enumerate(rows[i] or []):
                if c and '전용' in str(c) and j < len(rows[i + 1]) and area_of(rows[i + 1][j]): vals.add(area_of(rows[i + 1][j]))
    if len(vals) == 1:
        v = vals.pop()
        for u in us: u['area_m2'] = v; u['note'] = (u.get('note') or '') + ' 면적=별도 전용면적 표'

def _html_tables(xhtml):
    tabs = []
    for t in re.findall(r'<table.*?</table>', xhtml, re.S):
        grid = []
        for tr in re.findall(r'<tr.*?</tr>', t, re.S):
            row = []
            for m in re.finditer(r'<t[dh]([^>]*)>(.*?)</t[dh]>', tr, re.S):
                txt = H.unescape(re.sub(r'<[^>]+>', ' ', m.group(2))).replace('\r', ' ')
                txt = re.sub(r'[ \t]+', ' ', txt).strip()
                cs = int((re.search(r'colspan="(\d+)"', m.group(1)) or [0, 1])[1]); row += [txt] + [None] * (cs - 1)
            grid.append(row)
        tabs.append(grid)
    return tabs
def _rhwp_tables(path):
    """rhwp-python IR → 2D 표 그리드 리스트. (실패 시 예외)"""
    rhwp = _rhwp()
    if not rhwp: raise RuntimeError('rhwp unavailable')
    from rhwp.ir.nodes import TableBlock
    doc = rhwp.parse(str(path)); ir = doc.to_ir(); tabs, txt = [], doc.extract_text() or ''
    for b in ir.iter_blocks(scope='body'):
        if not isinstance(b, TableBlock): continue
        grid = [[None] * b.cols for _ in range(b.rows)]
        for cell in b.cells or []:
            parts = []
            for blk in (cell.blocks or []):
                t = getattr(blk, 'text', None) or (blk.get('text') if isinstance(blk, dict) else None)
                if t: parts.append(t)
            val = ' '.join(parts).strip() or None
            r0, c0 = cell.row, cell.col
            rs = getattr(cell, 'row_span', 1) or 1; cs = getattr(cell, 'col_span', 1) or 1
            for rr in range(r0, min(b.rows, r0 + rs)):
                for cc in range(c0, min(b.cols, c0 + cs)):
                    if grid[rr][cc] is None: grid[rr][cc] = val if (rr, cc) == (r0, c0) else None
        tabs.append(grid)
    return tabs, txt

def extract_hwp(path, kind, jeonse=False):
    """HWP/HWPX: rhwp-python 우선, 실패 시 hwp5html(HWP) / section XML(HWPX)."""
    method = None; tabs = []; txt = ''
    try:
        tabs, txt = _rhwp_tables(path); method = 'rhwp'
    except Exception as e_rhwp:
        if kind == 'hwp':
            d = Path(tempfile.mkdtemp())
            try:
                r = subprocess.run([shutil.which('hwp5html') or str(Path(sys.executable).parent / 'hwp5html'), '--output', str(d), str(path)], capture_output=True, timeout=120)
                x = (d / 'index.xhtml').read_text(errors='ignore') if (d / 'index.xhtml').exists() else ''
            finally: shutil.rmtree(d, ignore_errors=True)
            if not x: raise RuntimeError(f'rhwp 실패({e_rhwp}) + hwp5html 실패 ' + r.stderr.decode()[-200:])
            tabs = _html_tables(x); txt = re.sub(r'<[^>]+>', ' ', x); method = 'hwp5html'
        else:
            z = zipfile.ZipFile(path); xs = [z.read(n).decode('utf-8', 'ignore') for n in z.namelist() if re.match(r'Contents/section\d+\.xml', n)]
            x = '\n'.join(xs); tabs = []
            for t in re.findall(r'<hp:tbl.*?</hp:tbl>', x, re.S):
                grid = []
                for tr in re.findall(r'<hp:tr.*?</hp:tr>', t, re.S):
                    row = []
                    for tc in re.findall(r'<hp:tc.*?</hp:tc>', tr, re.S):
                        cs = int((re.search(r'colSpan="(\d+)"', tc) or [0, 1])[1]); row += [' '.join(re.findall(r'<hp:t>([^<]*)</hp:t>', tc))] + [None] * (cs - 1)
                    grid.append(row)
                tabs.append(grid)
            txt = re.sub(r'<[^>]+>', ' ', x); method = 'hwpx-xml'
    rent_tabs = [t for t in tabs if '보증금' in ' '.join(str(c) for r in t for c in r if c)]
    rmap = _room_area_map(rent_tabs); us = []
    for ti, t in enumerate(rent_tabs):
        for u in parse_table(t, txt, jeonse=jeonse):
            u['table_index'] = ti; us.append(u)
    us = _dedup_units(us)
    ex = addr.attach(us, txt)
    info = dict(tables=len(rent_tabs), method=method, rhwp=bool(method == 'rhwp'))
    structured = build_structured(kind=kind, method=method, units=us, info=info, text=txt, addresses=ex)
    return us, info, structured

def extract_ocr(path, kind, outdir, max_pages=12):
    """이미지형: 페이지 PNG → tesseract TSV 로 '보증금' 위치 찾아 표 영역 크롭 → 크롭 OCR → 숫자 후보(미검증)."""
    from PIL import Image
    outdir.mkdir(parents=True, exist_ok=True)
    if kind == 'image': imgs = [path]
    else:
        subprocess.run(['pdftoppm', '-r', '200', '-png', '-l', str(max_pages), str(path), str(outdir / 'p')], capture_output=True, timeout=300)
        imgs = sorted(outdir.glob('p-*.png'))
    us, crops = [], []
    for pi, img in enumerate(imgs, 1):
        tsv = subprocess.run(['tesseract', str(img), '-', '-l', 'kor+eng', '--psm', '6', 'tsv'], capture_output=True, timeout=180).stdout.decode('utf-8', 'ignore')
        words = [l.split('\t') for l in tsv.splitlines()[1:] if l.count('\t') >= 11 and l.split('\t')[11].strip()]
        lines = {}
        for w in words: lines.setdefault(tuple(w[1:5]), []).append(w)
        ltxt = [(min(int(w[7]) for w in ws), ''.join(w[11] for w in ws)) for ws in lines.values()]
        hits = [y for y, t in ltxt if re.search(r'보증|임대료|월세|사용료', t)]
        if not hits: continue
        im = Image.open(img); W, Hh = im.size
        top = max(0, min(hits) - 120); bot = min(Hh, top + int(Hh * 0.30))
        crop = outdir / f'crop_p{pi}.png'; im.crop((0, top, W, bot)).save(crop); crops.append(str(crop))
        txt = subprocess.run(['tesseract', str(crop), '-', '-l', 'kor+eng', '--psm', '6'], capture_output=True, timeout=180).stdout.decode('utf-8', 'ignore')
        hint = page_unit(' '.join(t for _, t in ltxt))
        for li, line in enumerate(txt.splitlines(), 1):
            toks = re.findall(r'\d{1,3}(?:[,.]\d{3})+|\d+\.\d+|\d+', line)
            area = next((float(t) for t in toks if re.fullmatch(r'\d{1,3}\.\d{1,3}', t) and 5 <= float(t) <= 200), None)
            money = [float(re.sub(r'[,.]', '', t)) for t in toks if re.fullmatch(r'\d{1,3}(?:[,.]\d{3})+', t) or re.fullmatch(r'\d{2,9}', t)]
            big = any(m >= 1e6 for m in money)        # 원 단위 보증금이 보이면 그 토큰에서 시작하는 쌍만 인정
            k = 0
            while k + 1 < len(money):
                if big and money[k] < 1e6: k += 1; continue
                d = to_manwon(money[k], None, 'dep', hint); r = to_manwon(money[k + 1], None, 'rent', hint)
                if 100 <= d <= 200000 and 0.5 <= r <= 600 and d > r * 5:
                    us.append(dict(unit_label=f'OCR p{pi} L{li} #{len(us)+1}', target='공통', area_m2=area, deposit=d, rent=r, supply=None,
                                   note=f'OCR 원문 "{line.strip()[:80]}" crop={crop}')); k += 2
                else: k += 1
    us = _dedup_units(us)
    info = dict(pages=len(imgs), crops=crops)
    structured = build_structured(kind=kind, method='tesseract-ocr', units=us, info=info, text='')
    return us, info, structured

def render_pages_for_agent(path, kind, outdir, max_pages=12):
    """OCR 끔(기본): 이미지형 공고는 숫자를 추출하지 않고 페이지 PNG 만 남김 → 에이전트가 직접 보고 units.py 로 기록."""
    outdir.mkdir(parents=True, exist_ok=True)
    if kind == 'image': imgs = [str(path)]
    else:
        subprocess.run(['pdftoppm', '-r', '150', '-png', '-l', str(max_pages), str(path), str(outdir / 'p')], capture_output=True, timeout=300)
        imgs = [str(x) for x in sorted(outdir.glob('p-*.png'))]
    info = dict(pages=len(imgs), page_images=imgs, note='image notice - read by agent (OCR disabled)')
    return [], info, build_structured(kind=kind, method='none', units=[], info=info, text='', page_images={i: x for i, x in enumerate(imgs, 1)})

# ---------------------------------------------------------------- 파이프라인
def _now(): return dt.datetime.now(hdb.KST).isoformat(timespec='seconds')
def save_units(con, src, iid, origin, units, doc_id):
    for u in units:
        con.execute('INSERT OR REPLACE INTO units(source,item_id,unit_label,target,area_m2,deposit,rent,note,origin,verified,supply,doc_id,lease_type,address) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (src, iid, u['unit_label'], u['target'], u['area_m2'], u['deposit'], u['rent'], u.get('note'), origin, 0, u.get('supply'), doc_id,
                     u.get('lease_type') or ('jeonse' if not u['rent'] else 'monthly'), u.get('address')))

def process_file(con, src, iid, f, cfg, force_ocr=False, lease_hint=None, force=False):
    name = f['name']; d = DOCS / src / iid; d.mkdir(parents=True, exist_ok=True)
    row = dict(source=src, item_id=iid, file_url=f['url'], file_name=name, processed_at=_now(), verified='auto', parser_version=PARSER_VERSION)
    if FORM.search(name) and not re.search(r'공고', name):
        row.update(status='skipped_form', kind=None, method='none'); return row, []
    safe = re.sub(r'[^\w.\-가-힣]', '_', name)[-80:]; p = d / safe
    # 로컬에 이미 같은 이름 파일이 있고 force 아니면 재다운로드 생략(sha 계산 후 캐시 판단)
    if not p.exists() or force_ocr:
        _curl(f['url'], p, f.get('referer'), timeout=cfg.get('http_timeout_sec', 40) * 2)
    elif p.exists() and p.stat().st_size == 0:
        _curl(f['url'], p, f.get('referer'), timeout=cfg.get('http_timeout_sec', 40) * 2)
    sha = hashlib.sha256(p.read_bytes()).hexdigest(); kind = classify(p, name)
    row.update(sha256=sha, size=p.stat().st_size, kind=kind, local_path=str(p))
    # 캐시: 같은 sha256 + 같은 parser_version + 성공 상태 → 재파싱 금지
    same = con.execute("""SELECT * FROM documents WHERE sha256=? AND parser_version=? AND status IN ('ok','no_table')
                          AND NOT (source=? AND item_id=? AND file_url=?) ORDER BY doc_id LIMIT 1""",
                       (sha, PARSER_VERSION, src, iid, f['url'])).fetchone()
    if same and not (force_ocr or force):
        us = [dict(r) for r in con.execute('SELECT * FROM units WHERE doc_id=?', (same['doc_id'],))]
        row.update(status='dup_hash', method=same['method'], summary=json.dumps(dict(copied_from=same['doc_id'], units=len(us)), ensure_ascii=False),
                   structured_json=same['structured_json'])
        return row, [(u['origin'], u) for u in us]
    # 자기 자신 이전 처리(같은 URL)가 현재 버전이면 재파싱 스킵 — process_item 레벨에서 주로 처리
    self_hit = con.execute("""SELECT * FROM documents WHERE source=? AND item_id=? AND file_url=? AND sha256=? AND parser_version=? AND status IN ('ok','no_table')""",
                           (src, iid, f['url'], sha, PARSER_VERSION)).fetchone()
    if self_hit and not (force_ocr or force):          # force: process_item 이 auto 행을 먼저 지우므로 반드시 재파싱
        us = [dict(r) for r in con.execute('SELECT * FROM units WHERE doc_id=?', (self_hit['doc_id'],))]
        row.update(status='ok' if us else 'no_table', method=self_hit['method'], summary=self_hit['summary'], structured_json=self_hit['structured_json'])
        return row, [(u.get('origin') or 'auto', u) for u in us]
    structured = None
    try:
        page_dir = d / (safe + '_pages')
        if kind == 'pdf-text' and not force_ocr:
            kw = {'jeonse': True} if lease_hint == 'jeonse' else {}
            us, info, structured = extract_pdf_text(p, page_outdir=page_dir, **kw); method, origin = 'pdfplumber', 'auto'
        elif kind in ('hwp', 'hwpx'):
            us, info, structured = extract_hwp(p, kind, **({'jeonse': True} if lease_hint == 'jeonse' else {})); method, origin = info.get('method') or ('hwp5html' if kind == 'hwp' else 'hwpx-xml'), 'auto'
        elif kind in ('pdf-image', 'image') and not (cfg.get('docs', {}).get('ocr') or force_ocr):
            us, info, structured = render_pages_for_agent(p, kind, page_dir, cfg.get('docs', {}).get('ocr_max_pages', 12)); method, origin = 'none', 'ocr'
            info['units'] = 0; info['sample'] = []
            row.update(status='needs_agent', method=method, summary=json.dumps({k: info[k] for k in info if k != 'page_images'}, ensure_ascii=False),
                       structured_json=json.dumps(structured, ensure_ascii=False, default=str))
            return row, []
        elif kind in ('pdf-image', 'image', 'pdf-text'):
            us, info, structured = extract_ocr(p, kind, d / (safe + '_ocr'), cfg.get('docs', {}).get('ocr_max_pages', 12)); method, origin = 'tesseract-ocr', 'ocr'
        else:
            row.update(status='unsupported', method='none'); return row, []
    except Exception as e:
        row.update(status='error', method=None, error=f'{type(e).__name__}: {e}'[:300]); return row, []
    info['units'] = len(us); info['sample'] = [f"{u['unit_label']} {u['area_m2']}㎡ {u['deposit']:g}/{u['rent']:g}" for u in us[:6]]
    if structured is not None: structured['sample'] = info['sample']
    row.update(status='ok' if us else 'no_table', method=method,
               summary=json.dumps({k: info[k] for k in info if k != 'page_images'}, ensure_ascii=False, default=str),
               structured_json=json.dumps(structured, ensure_ascii=False, default=str) if structured else None)
    return row, [(origin, u) for u in us]


def upsert_doc(con, row):
    cols = ['source', 'item_id', 'file_url', 'file_name', 'sha256', 'size', 'kind', 'status', 'method', 'summary', 'structured_json', 'parser_version', 'verified', 'local_path', 'processed_at', 'error']
    con.execute(f"INSERT INTO documents({','.join(cols)}) VALUES({','.join('?'*len(cols))}) ON CONFLICT(source,item_id,file_url) DO UPDATE SET "
                + ','.join(f'{c}=excluded.{c}' for c in cols[3:]), [row.get(c) for c in cols])
    return con.execute('SELECT doc_id FROM documents WHERE source=? AND item_id=? AND file_url=?', (row['source'], row['item_id'], row['file_url'])).fetchone()[0]

def process_item(con, src, iid, url, cfg, force=False, force_ocr=False, lease_hint=None):
    """한 공고의 첨부 전체 처리. 이미 처리 기록이 있으면(force 아니면) 네트워크 없이 skip.
    returns summary dict (+ address: 공고 단위 자동 주소 = 첨부 단일 주소 > 상세 페이지 '주택위치/소재지')"""
    if not force:
        rows = list(con.execute('SELECT parser_version, status FROM documents WHERE source=? AND item_id=?', (src, iid)))
        if rows and all((r['parser_version'] == PARSER_VERSION) or r['status'] in ('skipped_form', 'unsupported', 'no_attachment', 'dup_hash') for r in rows):
            return dict(skipped=True, reason='cache', parser_version=PARSER_VERSION)
    files = list_attachments(src, iid, url)
    page = _LAST_PAGE.pop(url, '')
    page_addr = addr.page_address(page) if src != 'socialhousing' else None   # 사회주택협회 페이지엔 다른 방 주소도 섞임 → 목록 '주소' 열 사용(collect)
    if not files:
        upsert_doc(con, dict(source=src, item_id=iid, file_url='', status='no_attachment', kind='none', method='none', processed_at=_now())); con.commit()
        return dict(files=0, address=page_addr)
    con.execute("DELETE FROM units WHERE source=? AND item_id=? AND origin IN ('auto','ocr')", (src, iid))
    res, used, doc_addr = [], set(), None
    for f in files[:cfg.get('docs', {}).get('max_files_per_item', 4)]:
        try: row, us = process_file(con, src, iid, f, cfg, force_ocr, lease_hint, force=force)
        except Exception as e:
            row, us = dict(source=src, item_id=iid, file_url=f['url'], file_name=f['name'], status='error', error=f'{type(e).__name__}: {e}'[:300], processed_at=_now()), []
        did = upsert_doc(con, row)
        for o, u in us:                          # 첨부 여러 개일 때 라벨 충돌 방지
            if u['unit_label'] in used: u['unit_label'] = f"{u['unit_label']} (파일{len(res)+1})"
            used.add(u['unit_label'])
        for origin in ('auto', 'ocr'):
            save_units(con, src, iid, origin, [u for o, u in us if o == origin], did)
        res.append((f['name'], row['status'], len(us)))
        try: doc_addr = doc_addr or (json.loads(row.get('structured_json') or '{}').get('meta') or {}).get('address')
        except Exception: pass
    con.commit()
    return dict(files=len(files), results=res, address=doc_addr or page_addr)   # 첨부(도로명 위주) > 상세 페이지

if __name__ == '__main__':
    con = hdb.connect(); a = sys.argv[1:]; cfg = hdb.load_cfg()
    if a and a[0] == 'process':
        r = con.execute('SELECT url FROM listings WHERE source=? AND item_id=? ORDER BY collected_date DESC LIMIT 1', (a[1], a[2])).fetchone()
        url = r[0] if r else {'sh': f'https://www.i-sh.co.kr/app/lay2/program/S48T1581C563/www/brd/m_247/view.do?multi_itm_seq=2&seq={a[2]}',
                              'soco': f'https://soco.seoul.go.kr/youth/bbs/BMSR00015/view.do?boardId={a[2]}&menuNo=400008'}[a[1]]
        print(json.dumps(process_item(con, a[1], a[2], url, cfg, force='--force' in a, force_ocr='--ocr' in a), ensure_ascii=False))
        for u in con.execute("SELECT origin,unit_label,target,area_m2,deposit,rent,supply FROM units WHERE source=? AND item_id=? AND origin<>'human'", (a[1], a[2])): print(tuple(u))
    else:
        q = 'SELECT doc_id,source,item_id,kind,status,method,file_name,summary FROM documents' + (' WHERE source=? AND item_id=?' if len(a) == 3 else '') + ' ORDER BY doc_id'
        for r in con.execute(q, tuple(a[1:3]) if len(a) == 3 else ()): print(dict(r))
