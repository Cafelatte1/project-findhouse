#!/usr/bin/env python3
"""수집 결과 마크다운 템플릿. run_daily / query.py 공통.
  from report import render_alert, render_listing_card, BASIS_LABEL
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
