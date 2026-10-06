#!/usr/bin/env python3
"""수집 결과 마크다운 템플릿. run_daily / query.py 공통.
  render_chat  : 채팅 알림(기본) — 한 줄 요약 + 임대유형별 블록(월세/전세) + 📋 지원 프로그램 + 수집 장애
  render_alert : 카드형(레거시, --format cards)
  from report import render_chat, render_alert, render_listing_card, BASIS_LABEL
"""
from __future__ import annotations
import json, re
from pathlib import Path

BASIS_LABEL = {
    'human': '사람확인',
    'auto': '자동추출(PDF/HWP표) · 원문확인 권장',
    'ocr': 'OCR 미검증',
    'none': '-',
}
SECTION = {
    'match': ('🆕 부합', '조건에 맞는 공고'),
    'near': ('⚠ 근소초과', '보증금·월세가 허용폭 이내로 벗어남 (참고)'),
    'due': ('⏰ 마감 임박', '접수 마감 D-3 이내'),
    'unverified': ('🧪 자동추출(미검증·OCR)', '크롭 이미지를 보고 units.py 로 확인 기록'),
    'unknown': ('🔎 가격 미확인', '첨부에서 임대료 표를 못 찾음(이미지형 포함) — 공고문/페이지 이미지 확인 후 units.py 로 기록'),
    'fail': ('❗ 수집처 장애', '소스별 예외·구조 변경'),
    'summary': ('📋 오늘 부합·근소초과 전체', '알림과 별개 요약'),
}

def money(r):
    if r.get('deposit') is None: return '확인 필요'
    return f"{r['deposit']:g}만 / {r['rent']:g}만"

def loc(r):
    g = (r.get('gu') or '').strip(); s = (r.get('station') or '').strip()
    if g and s: return f'{g} · {s}'
    return g or s or '확인 필요'

def title_link(r):
    name = r.get('name') or (r.get('title') or '')[:50]
    url = r.get('url') or ''
    return f'[{name}]({url})' if url else name

def period(r):
    a, b = r.get('apply_start') or '?', r.get('apply_end') or '?'
    return f'{a} ~ {b}'

def option_note(r):
    """fit_reason / note 에서 전환·보증금비율 옵션 한 줄."""
    fr = r.get('fit_reason') or ''
    m = re.search(r'보증금\d+%|최대전환|전환\([+\-]\)|보증금→|임대료→', fr)
    return m.group(0) if m else ''

def render_listing_card(r, *, show_fit=False):
    """한 공고 카드 (불릿 목록)."""
    area = r.get('area_m2'); area_s = f'{area:g}㎡' if area else '확인 필요'
    rpm = r.get('rent_per_m2_won'); rpm_s = f'{rpm:,}원/㎡' if rpm else '-'
    opt = option_note(r)
    price = money(r) + (f' ({opt})' if opt else '')
    basis = BASIS_LABEL.get(r.get('fit_basis') or 'none', r.get('fit_basis') or '-')
    fit_tag = f"**{r['fit']}** · " if show_fit else ''
    lines = [
        f"- **{title_link(r)}**",
        f"  - 위치: {loc(r)} · 전용 {area_s} · {price}" + (f' · {rpm_s}' if rpm else ''),
        f"  - 접수: {period(r)} · 상태: {r.get('status') or '-'}",
        f"  - 판정: {fit_tag}{basis}",
    ]
    if r.get('fit_reason'):
        lines.append(f"  - 사유: {r['fit_reason']}")
    crops = []
    for cs in r.get('crops') or []:
        crops += cs if isinstance(cs, list) else [cs]
    if crops:
        lines.append(f"  - 크롭: {', '.join(str(c) for c in crops[:4])}")
    return '\n'.join(lines)

def _header(cd, cfg, res):
    md, mr = cfg['max_deposit_manwon'], cfg['max_rent_manwon']
    nd, nr = md + cfg['near_tolerance_deposit_manwon'], mr + cfg['near_tolerance_rent_manwon']
    docs = res.get('docs') or []
    doc_sec = round(sum(d.get('sec', 0) for d in docs), 1)
    src = res.get('source_sec') or {}
    src_s = ', '.join(f'{k} {v}s' for k, v in src.items()) if src else '-'
    return [
        f"# 서울 청년 월세 임대 알림 — {cd} (KST)",
        '',
        f"| 항목 | 값 |",
        f"|---|---|",
        f"| 부합 기준 | 보증금 ≤ **{md:g}만원**, 월세 ≤ **{mr:g}만원** |",
        f"| 근소초과 | ≤ {nd:g}만 / ≤ {nr:g}만 |",
        f"| 수집 | `{res.get('counts')}` |",
        f"| 소요 | **{res.get('elapsed_sec')}s** (소스 {src_s} · 첨부 {len(docs)}건 {doc_sec}s) |",
        f"| 제외 지역 | {', '.join(cfg.get('exclude_gu') or []) or '없음'} |",
    ]

def render_alert(*, cd, cfg, res, sections, quiet_msg='**알릴 것 없음**'):
    """sections: list of (key, rows) where key in SECTION. fails dict goes as key='fail' with list of '- k: v' strings or dict."""
    out = _header(cd, cfg, res)
    any_sec = False
    for key, rows in sections:
        if not rows: continue
        any_sec = True
        title, blurb = SECTION.get(key, (key, ''))
        out += ['', f'## {title} ({len(rows) if not isinstance(rows, dict) else len(rows)}건)', f'*{blurb}*', '']
        if key == 'fail':
            if isinstance(rows, dict):
                out += [f'- **{k}**: {v}' for k, v in rows.items()]
            else:
                out += list(rows)
        else:
            show = key == 'summary'
            out += [render_listing_card(r, show_fit=show) for r in rows]
            out.append('')
    if not any_sec:
        out += ['', quiet_msg]
    out += ['', '---', f'*생성: housing/report.py · DB `{Path("housing.db").name}` · `--no-mark` 이면 알림상태 미갱신*']
    return '\n'.join(out)

def render_query(cd, rows, *, title=None):
    out = [f"# 조회 — collected_date={cd} · {len(rows)}건", '']
    if title: out = [f'# {title}', ''] + out[1:]
    if not rows:
        out.append('_결과 없음_'); return '\n'.join(out)
    by = {}
    for r in rows:
        by.setdefault(r.get('fit') or '?', []).append(r)
    order = ['match', 'near', 'unverified', 'unknown', 'closed_match', 'closed', 'no', 'excluded_region', 'irrelevant']
    for k in order + [x for x in by if x not in order]:
        lst = by.get(k) or []
        if not lst: continue
        out += [f'## {k} ({len(lst)}건)', ''] + [render_listing_card(r, show_fit=True) for r in lst] + ['']
    return '\n'.join(out)


# ───────────────────────── 채팅 알림 (기본) ─────────────────────────
LEASE_LABEL = {'monthly': '월세', 'jeonse': '전세'}
TARGET_LABEL = {'youth': '청년', 'newlywed': '신혼부부'}
CHAT_SECTION = [            # (키, 제목, 요약줄 라벨)
    ('match', '✅ **조건 부합**', '부합'),
    ('near', '⚠️ **근소 초과**', '근소'),
    ('due', '⚠️ **마감 임박**', '마감 임박'),
    ('notice', '⚠️ **공고 단위 확인**', '공고'),          # HUG 든든전세 등: 주택별 가격은 공고 원문(호수·보증금 규칙·기간만)
    ('unknown', '⚠️ **가격 미확인**', '가격 미확인'),
    ('unverified', '⚠️ **OCR 미검증**', '미검증'),
]

def man(v):
    """만원 금액 → '1억 8,410만' / '2,000만' / '87.5만'."""
    if v is None: return '확인 필요'
    v = float(v)
    if v >= 10000:
        eok = int(v // 10000); rest = round(v - eok * 10000, 1)
        return f'{eok}억' + (f' {rest:,g}만' if rest else '')
    return f'{v:,g}만'

def _best(r, lease):
    try: return (json.loads(r.get('best_json') or '{}') or {}).get(lease)
    except Exception: return None

def _conv_like(b, cfg):
    j = cfg.get('jeonse') or {}
    return bool(b and b.get('rent') and b['rent'] <= j.get('jeonse_like_max_rent_manwon', 10)
                and b['deposit'] >= j.get('jeonse_like_min_deposit_manwon', 5000))

def _price_line(r, lease, cfg):
    b = _best(r, lease)
    if not b:
        if lease == 'monthly' and r.get('deposit') is not None and r.get('rent'):
            b = dict(deposit=r['deposit'], rent=r['rent'], area_m2=r.get('area_m2'))
        else: return None
    reason = r.get('reason_monthly' if lease == 'monthly' else 'reason_jeonse') or ''
    opt = option_note(dict(fit_reason=reason))
    if lease == 'jeonse' and b.get('conv'):
        s = f"🔁 전환 · 보증금 {man(b['deposit'])} / 월세 {man(b['rent'])}"
    elif lease == 'jeonse':
        s = f"전세 {man(b['deposit'])}"
    else:
        s = f"보증금 {man(b['deposit'])} / 월세 {man(b['rent'])}"
        if _conv_like(b, cfg): s = '🔁 전환 · ' + s
    if opt: s += f' ({opt})'
    if '[자동추출]' in reason: s += ' · 원문 확인 권장'
    return s

def _loc_line(r, lease):
    b = _best(r, lease) or {}
    area = b.get('area_m2') or (r.get('area_m2') if lease == 'monthly' or not r.get('best_json') else None)
    parts = [x for x in ((r.get('gu') or '').strip(), (r.get('station') or '').strip()) if x]
    if area: parts.append(f'{area:g}㎡')
    return ' · '.join(parts)

def _period(r):
    a, b = r.get('apply_start'), r.get('apply_end')
    if not a and not b: return '접수 기간 원문 확인'
    return f"접수 {a + ' ' if a else ''}~{' ' + b if b else ''}"

def _item(r, lease, cfg, *, show_target):
    name = r.get('name') or (r.get('title') or '')[:50]
    head = f"• 🏠 **[{name}]({r['url']})**" if r.get('url') else f'• 🏠 **{name}**'
    if show_target:      # 대상이 둘 다 선택된 경우만: 판정된 주택형의 공급대상 > 공고 제목 표기
        bt = (_best(r, lease) or {}).get('target')
        tg = bt if bt in TARGET_LABEL.values() else r.get('targets')
        if tg: head += f' · {tg}'
    lines = [head]
    loc_s = _loc_line(r, lease)
    if loc_s: lines.append(f'  - {loc_s}')
    pl = _price_line(r, lease, cfg)
    if pl: lines.append(f'  - {pl}')
    if r.get('extra_note'): lines.append(f"  - {r['extra_note']}")
    per = _period(r)
    if r.get('is_due_soon'): per += ' · 마감 임박'
    lines.append(f'  - {per}')
    if r.get('elig_note'): lines.append(f"  - 자격: {r['elig_note']}")
    crops = []
    for cs in r.get('crops') or []: crops += cs if isinstance(cs, list) else [cs]
    if crops: lines.append(f"  - 이미지: {', '.join(str(c) for c in crops[:4])}")
    return '\n'.join(lines)

def _criteria(lease, cfg):
    if lease == 'monthly':
        return f"• 기준: 보증금 ≤{man(cfg['max_deposit_manwon'])} · 월세 ≤{man(cfg['max_rent_manwon'])}"
    j = cfg['jeonse']
    return f"• 기준: 전세 보증금 ≤{man(j['max_deposit_manwon'])}"

def chat_title(cfg):
    tg = '·'.join(TARGET_LABEL[t] for t in ('youth', 'newlywed') if t in (cfg.get('targets') or ['youth']))
    lt = '·'.join(LEASE_LABEL[t] for t in ('monthly', 'jeonse') if t in (cfg.get('lease_types') or ['monthly']))
    return f'서울 {tg} {lt} 수집'

def summary_line(blocks, support, fails):
    parts = []
    for lease, secs in blocks.items():
        cnt = [f'{lab} {len(secs.get(k) or [])}' for k, _, lab in CHAT_SECTION if secs.get(k)]
        if cnt: parts.append(f'{LEASE_LABEL[lease]} ' + ' · '.join(cnt))
    if support: parts.append(f'지원 프로그램 {len(support)}')
    if fails: parts.append(f'수집 장애 {len(fails)}')
    return ' / '.join(parts)

def render_chat(*, cd, cfg, blocks, support=None, fails=None, quiet_msg='**알릴 것 없음**'):
    """blocks: {'monthly': {'match': rows, 'near': rows, 'due': rows, 'unknown': rows, 'unverified': rows}, 'jeonse': {...}}
    선택한 임대유형 순서(월세→전세)로, 내용이 있는 블록만 출력. 블록마다 자체 기준줄과 ✅/⚠️ 섹션.
    대상 태그는 대상이 둘 다 선택된 경우에만 붙인다. 알릴 것이 없으면 quiet_msg만."""
    support = support or []; fails = fails or {}
    order = [t for t in ('monthly', 'jeonse') if t in (cfg.get('lease_types') or ['monthly'])]
    blocks = {t: blocks.get(t) or {} for t in order if any(blocks.get(t, {}).values())}
    if not blocks and not support and not fails: return quiet_msg
    show_target = len(cfg.get('targets') or ['youth']) > 1
    out = [f'**{chat_title(cfg)}** · {cd}', summary_line(blocks, support, fails)]
    multi = len(order) > 1
    for lease, secs in blocks.items():
        out.append('')
        if multi: out.append(f'**{LEASE_LABEL[lease]}**')
        out.append(_criteria(lease, cfg))
        if lease == 'jeonse' and any((_best(r, 'jeonse') or {}).get('conv') for k in ('match', 'near') for r in secs.get(k) or []):
            j = cfg['jeonse']
            out.append(f"• 🔁 전환: 월세 ≤{man(j['jeonse_like_max_rent_manwon'])} · 보증금 ≥{man(j['jeonse_like_min_deposit_manwon'])} 옵션도 전세 기준으로 판정")
        for key, title, _ in CHAT_SECTION:
            rows = secs.get(key) or []
            if not rows: continue
            out += ['', f'{title} ({len(rows)})', '']
            out += [_item(r, lease, cfg, show_target=show_target) for r in rows]
    if support:
        out += ['', f'📋 **지원 프로그램** ({len(support)})', '']
        for r in support:
            name = (r.get('title') or '')[:60]
            head = f"• [{name}]({r['url']})" if r.get('url') else f'• {name}'
            per = _period(r) + (' · 마감 임박' if r.get('is_due_soon') else '')
            if r.get('extra_note'): per += f" · {r['extra_note']}"
            out += [head, f'  - {per}', '  - 입주자가 주택을 구해 신청 · 가격 판정 없음']
    if fails:
        out += ['', f'⚠️ **수집 장애** ({len(fails)})', ''] + [f'• {k}: {str(v)[:160]}' for k, v in fails.items()]
    return '\n'.join(out)
