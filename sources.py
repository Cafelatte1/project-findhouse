"""소스별 목록 수집기. 각 함수는 (items, error) 를 반환하지 않고 예외를 던지며, 호출측(collect.py)이 소스별로 격리한다.
단독 실행: python sources.py sh|seoulportal|lh|soco"""
import re, html, subprocess, time, asyncio, sys, json, datetime as dt
UA = 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36'
KST = dt.timezone(dt.timedelta(hours=9))

def curl(url, data=None, timeout=40, retries=2, encoding='utf-8'):
    last = None
    for i in range(retries + 1):
        a = ['curl', '-s', '-L', '--fail', '-m', str(timeout), '-A', UA, url] + (['--data', data] if data else [])
        r = subprocess.run(a, capture_output=True)
        if r.returncode == 0 and r.stdout: return r.stdout.decode(encoding, 'ignore')
        last = f'curl rc={r.returncode} {url[:80]}'; time.sleep(2 * (i + 1))
    raise RuntimeError(last)

def clean(s): return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()
def norm_date(s):
    m = re.search(r'(20\d\d)[.\-/](\d{1,2})[.\-/](\d{1,2})', s or '')
    return f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else None

def sh(cfg):
    out = {}
    for p in range(1, cfg['pages']['sh'] + 1):
        s = curl(f'https://www.i-sh.co.kr/app/lay2/program/S48T1581C563/www/brd/m_247/list.do?multi_itm_seq=2&page={p}', timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
        for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', s, re.S):
            seq = re.search(r"seq[=,'\"(\s]*(\d{6})|(\d{6})", m.group(1)); t = clean(m.group(1))
            d = re.search(r'(20\d\d-\d\d-\d\d)', t)
            if not seq or not d: continue
            sid = seq.group(1) or seq.group(2)
            title = re.sub(r'^\d+\s+', '', t[:t.find(d.group(1))]).strip()
            title = re.sub(r'\s+\S*(부|센터)\s*$', '', title)
            out[sid] = dict(source='sh', item_id=sid, title=title, posted=d.group(1), category=None, status=None,
                            url=f'https://www.i-sh.co.kr/app/lay2/program/S48T1581C563/www/brd/m_247/view.do?multi_itm_seq=2&seq={sid}')
    if not out: raise RuntimeError('SH 목록 파싱 0건')
    return list(out.values())

def seoulportal(cfg):
    out = {}
    for cp in range(1, cfg['pages']['seoulportal'] + 1):
        s = curl(f'https://housing.seoul.go.kr/site/main/sh/publicLease/list?cp={cp}&supplyType=publicLease', timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
        s = re.sub(r'<!--.*?-->', '', s, flags=re.S)     # 셀 안 HTML 주석(<!-- 2021-01-25 클래스 수정 -->)이 게시일로 잡히던 버그
        for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', s, re.S):
            c = [clean(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.S)]
            if len(c) < 7 or not c[0].isdigit(): continue
            link = re.search(r'href="(https?://www\.i-sh\.co\.kr/[^"]+)"', m.group(1))
            out[c[0]] = dict(source='seoulportal', item_id=c[0], category=c[1], title=c[2], posted=norm_date(c[3]),
                             announce=norm_date(c[4]), status=c[5], url=html.unescape(link.group(1)) if link else 'https://housing.seoul.go.kr/site/main/sh/publicLease/list')
    if not out: raise RuntimeError('서울주거포털 파싱 0건')
    return list(out.values())

LH_LIST = 'https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do'
LH_OPEN = ('접수중', '공고중')
def _lh_query(days, upp='061339', cnp='11', name=''):
    today = dt.datetime.now(KST).date(); d = today - dt.timedelta(days=days)
    return (f'mi=1026&currPage=1&srchY=Y&srchUppAisTpCd={upp}&uppAisTpCd={upp}&cnpCd={cnp}&panSs=&schTy=0'
            f'&startDt={d}&endDt={today}&panStDt={d:%Y%m%d}&panEdDt={today:%Y%m%d}&listCo=100&mvinQf=0&panNm={name}')
def _lh_rows(s):
    """LH 목록 HTML → (items dict, td 행 수). c[3]=지역."""
    out = {}; td_rows = 0
    for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', s, re.S):
        c = [clean(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.S)]
        ids = re.search(r'data-id1="([^"]*)"[^>]*data-id2="([^"]*)"[^>]*data-id3="([^"]*)"[^>]*data-id4="([^"]*)"', m.group(1))
        if len(c) >= 8: td_rows += 1
        if len(c) < 8 or not ids: continue
        pan, ccr, upp, ais = ids.groups()
        out[pan] = dict(source='lh', item_id=pan, category=c[1], title=re.sub(r'\s*\d+일전$', '', c[2]).strip(), region=c[3],
                        posted=norm_date(c[5]), apply_end=norm_date(c[6]), status=c[7],
                        url=f'https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancView.do?panId={pan}&ccrCnntSysDsCd={ccr}&uppAisTpCd={upp}&aisTpCd={ais}&mi=1026')
    return out, td_rows
def lh(cfg):
    data = _lh_query(cfg['pages']['lh_lookback_days'])
    s = curl(LH_LIST, data, timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
    if '공고명' not in s: raise RuntimeError('LH 응답에 목록 헤더 없음(구조 변경 의심)')
    out, td_rows = _lh_rows(s)
    for it in out.values(): it.pop('region', None)
    if td_rows and not out: raise RuntimeError(f'LH 목록 행 {td_rows}개인데 파싱 0건 — 구조 변경 의심(data-id 속성)')
    roll = cfg['pages'].get('lh_rolling_days') or 0
    if roll > cfg['pages']['lh_lookback_days']:   # 연중 수시모집(게시일이 오래됐지만 지금 접수중) 보강
        try:
            r2, _ = _lh_rows(curl(LH_LIST, _lh_query(roll), timeout=cfg['http_timeout_sec'], retries=cfg['http_retries']))
            for pan, it in r2.items():
                if pan not in out and it['status'] in LH_OPEN: it.pop('region', None); it['rolling'] = True; out[pan] = it
        except Exception: pass
    if not out:   # 서울 0건: 전국 카나리 조회로 '진짜 0건'과 '구조 변경/차단' 구분
        c2 = curl(LH_LIST, data.replace('cnpCd=11', 'cnpCd='), timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
        n2 = len(re.findall(r'data-id1="', c2))
        if n2 == 0: raise RuntimeError('LH 서울·전국 모두 0건 — 구조 변경/차단 의심(전국 60일 0건은 비정상)')
    return list(out.values())

async def _launch(p):
    """Chromium 실행: CHROME_PATH 환경변수 > playwright 번들 chromium > 시스템 google-chrome/chromium."""
    import os, shutil
    args = dict(headless=True, args=['--no-sandbox'])
    exe = os.environ.get('CHROME_PATH')
    if exe: return await p.chromium.launch(executable_path=exe, **args)
    try: return await p.chromium.launch(**args)
    except Exception:
        for n in ('google-chrome', 'chromium', 'chromium-browser'):
            if shutil.which(n): return await p.chromium.launch(executable_path=shutil.which(n), **args)
        raise

async def _soco(cfg):
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as p:
        b = await _launch(p)
        try:
            pg = await b.new_page()
            for i in range(1, cfg['pages']['soco'] + 1):
                for attempt in range(cfg['http_retries'] + 1):
                    try:
                        await pg.goto(f'https://soco.seoul.go.kr/youth/bbs/BMSR00015/list.do?menuNo=400008&pageIndex={i}', wait_until='networkidle', timeout=60000)
                        await pg.wait_for_selector('table tbody tr td', timeout=15000)
                        break
                    except Exception:
                        if attempt == cfg['http_retries']: raise
                        await asyncio.sleep(3)
                rows = await pg.eval_on_selector_all('table tbody tr', "els=>els.map(e=>[...e.querySelectorAll('td')].map(t=>t.innerText.trim()).concat([(e.innerHTML.match(/boardId=(\\d+)/)||[])[1]]))")
                for r in rows:
                    if len(r) >= 7 and r[6]:
                        out[r[6]] = dict(source='soco', item_id=r[6], category=r[1], title=r[2], posted=norm_date(r[3]),
                                         apply_start=norm_date(r[4]), operator=r[5], status=None,
                                         url=f'https://soco.seoul.go.kr/youth/bbs/BMSR00015/view.do?boardId={r[6]}&menuNo=400008')
        finally:
            await b.close()
    if not out: raise RuntimeError('청년안심 목록 0건')
    return list(out.values())
def soco(cfg): return asyncio.run(asyncio.wait_for(_soco(cfg), timeout=240))

def socialhousing(cfg):
    """한국사회주택협회 '입주자 모집중' 게시판 (SH 게시판에 안 올라오는 민간/LH 사회주택 보완). 서울·모집중만 상세에서 게시일 확인."""
    out = {}
    for pg in range(1, cfg['pages'].get('socialhousing', 2) + 1):
        s = curl(f'https://socialhousing.kr/room?listStyle=list&page={pg}', timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
        for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', s, re.S):
            tds = re.findall(r'<td[^>]*>(.*?)</td>', m.group(1), re.S); a = re.search(r'href="/room/(\d+)', m.group(1))
            if len(tds) < 8 or not a: continue
            c = [clean(x) for x in tds]
            title = clean(re.search(r'<a [^>]*>(.*?)<span class="wrp"', tds[1], re.S).group(1)) if '<span class="wrp"' in tds[1] else c[1]
            gu = re.search(r'서울\S*\s+(\S+구)', c[2])
            out[a.group(1)] = dict(source='socialhousing', item_id=a.group(1), title=title, status=c[0] or None, address=c[2], gu=gu.group(1) if gu else None,
                                   category=c[6], operator=c[7], price_band=f'{c[4]} / {c[5]}', posted=None,
                                   url=f'https://socialhousing.kr/room/{a.group(1)}')
    if not out: raise RuntimeError('사회주택협회 목록 파싱 0건')
    today = dt.datetime.now(KST).date()
    for it in out.values():
        if it['status'] != '모집중' or '서울' not in (it['address'] or ''): continue
        try:
            d = norm_date((re.search(r'(20\d\d\.\d\d\.\d\d)\s+\d\d:\d\d', curl(it['url'], timeout=cfg['http_timeout_sec'], retries=1)) or [None, None])[1])
        except Exception: d = None
        it['posted'] = d
        if d and (today - dt.date.fromisoformat(d)).days > cfg.get('socialhousing_max_age_days', 180):
            it['status'] = f'오래된 게시({d}, 모집 여부 확인 필요)'
    return list(out.values())

def lh_support(cfg):
    """LH 전세임대(입주자가 집을 구해오는 지원형) — 전국 단위 수시모집이 많아 지역=서울/전국 + 접수중만. 공고 단위."""
    s = curl(LH_LIST, _lh_query(cfg['pages'].get('lh_rolling_days') or 365, upp='13', cnp='', name='전세임대'),
             timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'])
    if '공고명' not in s: raise RuntimeError('LH 응답에 목록 헤더 없음(구조 변경 의심)')
    out, _ = _lh_rows(s)
    res = []
    for it in out.values():
        if it['category'] != '전세임대' or it['status'] not in LH_OPEN or not re.search(r'서울|전국', it['region'] or ''): continue
        it['lease'] = 'support'; it['extra_note'] = f"지역 {it.pop('region')}"; res.append(it)
    return res

HUG_LIST = 'https://www.khug.or.kr/jeonse/web/s07/s070102.jsp'
def hug(cfg):
    """HUG 든든전세주택(안심전세포털). 공고 PDF 1건 단위: 서울 공급 호수·보증금 규칙·접수기간·자격. 주택별 가격은 수집하지 않음."""
    import tempfile, os
    s = curl(HUG_LIST, timeout=cfg['http_timeout_sec'], retries=cfg['http_retries'], encoding='cp949')
    files = list(dict.fromkeys(re.findall(r'homepage_file_upload/(jeonse_notice_(\d{6})\.pdf)', s)))
    if not files: raise RuntimeError('HUG 목록에 공고 PDF 링크 없음(구조 변경 의심)')
    res = []
    for fn, ymd in files[:cfg['pages'].get('hug', 1)]:
        url = f'https://www.khug.or.kr/hug/homepage_file_upload/{fn}'
        fd, tmp = tempfile.mkstemp(suffix='.pdf'); os.close(fd)
        try:
            subprocess.run(['curl', '-s', '-L', '--fail', '-m', str(cfg['http_timeout_sec'] * 2), '-A', UA, '-o', tmp, url], check=True, capture_output=True)
            txt = subprocess.run(['pdftotext', '-l', '6', tmp, '-'], capture_output=True).stdout.decode('utf-8', 'ignore')
        finally: os.unlink(tmp)
        res.append(dict(source='hug', item_id=ymd, url=url, category='든든전세주택', lease='jeonse', **parse_hug(txt)))
    return res
def parse_hug(txt):
    """HUG 공고 PDF 텍스트 → 제목·공고일·접수기간·서울 호수·보증금 규칙·자격(원문 문구만)."""
    t = txt.replace('\n', '|')
    title = re.sub(r'\s*\[[\d.\s]+\]\s*$', '', clean(txt.strip().split('\n')[0])) or 'HUG 든든전세주택 입주자 모집 공고'
    m = re.search(r'모집공고일은\s*(20\d\d)\.(\d{1,2})\.(\d{1,2})', txt) or re.search(r'\[(20\d\d)\.(\d{1,2})\.(\d{1,2})\]', txt)
    posted = f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else None
    yr = int(m.group(1)) if m else dt.datetime.now(KST).year
    win = re.findall(r'신청접수.{0,80}?(\d{1,2})\.(\d{1,2})\([^)]\)\|?\s*\d\d:\d\d\|?\s*~\|?\s*(\d{1,2})\.(\d{1,2})\([^)]\)', t)
    def d(mo, da): return f'{yr}-{int(mo):02d}-{int(da):02d}'
    starts = sorted({d(a, b) for a, b, _, _ in win}); ends = sorted({d(c, e) for _, _, c, e in win})
    notes = []
    sm = re.search(r'서울\s*([\d,]+)\s*호', txt)
    if sm: notes.append(f'서울 {sm.group(1)}호')
    dm = re.search(r'시중\s*(전세)?\s*시세의\s*(\d+)%\s*이하', txt)
    if dm: notes.append(f'보증금 시세 {dm.group(2)}% 이하')
    if len(ends) > 1: notes.append('마감 표기 상이(' + '/'.join(e[5:] for e in ends) + ', 이른 날 기준 준비)')
    em = re.search(r'\(입주자격\)\s*([^\n]*?무주택세대구성원)', txt)
    return dict(title=title, posted=posted, apply_start=starts[0] if starts else posted, apply_end=ends[-1] if ends else None,
                status=None, extra_note=' · '.join(notes) or None, elig=(clean(em.group(1)) if em else None))

ALL = {'sh': sh, 'seoulportal': seoulportal, 'lh': lh, 'soco': soco, 'socialhousing': socialhousing, 'hug': hug, 'lh_support': lh_support}
NEEDS = {'hug': 'jeonse', 'lh_support': 'support'}    # 설정에서 해당 유형을 켰을 때만 수집
NOTICE_LEVEL = ('hug',)                                # 공고 단위(주택별 가격 미수집) → 알림 '공고 단위 확인'
if __name__ == '__main__':
    import hdb
    cfg = hdb.load_cfg(); name = sys.argv[1]; t = time.time()
    r = ALL[name](cfg); print(name, len(r), f'{time.time()-t:.1f}s'); print(json.dumps(r[:3], ensure_ascii=False, indent=1))
