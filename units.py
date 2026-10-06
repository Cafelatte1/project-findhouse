#!/usr/bin/env python3
"""공고문에서 확인한 주택형/가격/일정을 DB(item_meta, units)에 기록. 금액은 만원 단위(원 단위 입력 시 --won).
  meta : python units.py meta <source> <item_id> --name "단지명" --gu 마포구 --station 합정역 --apply-start YYYY-MM-DD --apply-end YYYY-MM-DD
  unit : python units.py unit <source> <item_id> "26형 보증금40%" --area 21.5 --deposit 8000 --rent 55 [--target 청년|신혼부부|공통] [--won]
         전세: python units.py unit <source> <item_id> "59A 전세" --area 59 --deposit 18410 --lease-type jeonse   (--rent 생략=0)
  list : python units.py list [source item_id]
  verify: python units.py verify <source> <item_id> ["라벨" ...]   (자동추출 값을 원문 대조 후 human 으로 승격; 라벨 생략 시 전체)"""
import sys, json, argparse, datetime as dt
import hdb

def to_manwon(v, won): return None if v is None else (round(v / 10000, 4) if won else v)
def put_meta(con, src, iid, **kw):
    kw = {k: v for k, v in kw.items() if v is not None}
    con.execute('INSERT OR IGNORE INTO item_meta(source,item_id) VALUES(?,?)', (src, iid))
    for k, v in kw.items(): con.execute(f'UPDATE item_meta SET {k}=? WHERE source=? AND item_id=?', (v, src, iid))
    con.execute('UPDATE item_meta SET verified_at=? WHERE source=? AND item_id=?', (dt.datetime.now(hdb.KST).isoformat(timespec='seconds'), src, iid))
def put_unit(con, src, iid, label, area, dep, rent, target='청년', note=None, lease_type=None):
    lease_type = lease_type or ('jeonse' if not rent else 'monthly')
    con.execute("INSERT OR REPLACE INTO units(source,item_id,unit_label,target,area_m2,deposit,rent,note,origin,verified,lease_type) VALUES(?,?,?,?,?,?,?,?,'human',1,?)",
                (src, iid, label, target, area, dep, rent or 0, note, lease_type))
def verify(con, src, iid, labels=None):
    """자동추출(auto/ocr) 값을 원문과 대조 확인했을 때: 사람확인(human)으로 승격 + documents.verified='human'."""
    q = "SELECT * FROM units WHERE source=? AND item_id=? AND origin IN ('auto','ocr')"; n = 0
    for u in con.execute(q, (src, iid)).fetchall():
        if labels and u['unit_label'] not in labels: continue
        put_unit(con, src, iid, u['unit_label'], u['area_m2'], u['deposit'], u['rent'], u['target'], (u['note'] or '') + f" (확인:{u['origin']}→human)", u['lease_type'] if 'lease_type' in u.keys() else None); n += 1
    con.execute("UPDATE documents SET verified='human' WHERE source=? AND item_id=?", (src, iid)); return n

if __name__ == '__main__':
    con = hdb.connect(); a = sys.argv[1:]
    if not a or a[0] == 'list':
        q = 'SELECT u.*, m.name, m.gu, m.apply_start, m.apply_end FROM units u LEFT JOIN item_meta m USING(source,item_id)' + (' WHERE u.source=? AND u.item_id=?' if len(a) == 3 else '')
        for r in con.execute(q, tuple(a[1:3]) if len(a) == 3 else ()): print(dict(r))
    elif a[0] == 'verify': n = verify(con, a[1], a[2], a[3:] or None); con.commit(); print(f'verified {n}')
    elif a[0] == 'meta':
        ap = argparse.ArgumentParser(); ap.add_argument('source'); ap.add_argument('item_id')
        for f in ('name', 'gu', 'station', 'housing_type', 'apply_start', 'apply_end', 'supply_count', 'eligibility', 'note'): ap.add_argument('--' + f.replace('_', '-'))
        x = ap.parse_args(a[1:]); put_meta(con, x.source, x.item_id, **{k: v for k, v in vars(x).items() if k not in ('source', 'item_id')}); con.commit(); print('ok')
    elif a[0] == 'unit':
        ap = argparse.ArgumentParser(); ap.add_argument('source'); ap.add_argument('item_id'); ap.add_argument('label')
        ap.add_argument('--area', type=float); ap.add_argument('--deposit', type=float, required=True); ap.add_argument('--rent', type=float, default=0, help='월세(만원). 전세는 생략(0)')
        ap.add_argument('--lease-type', choices=('monthly', 'jeonse'), help='기본: 월세 0이면 jeonse')
        ap.add_argument('--target', default='청년'); ap.add_argument('--note'); ap.add_argument('--won', action='store_true')
        x = ap.parse_args(a[1:]); put_unit(con, x.source, x.item_id, x.label, x.area, to_manwon(x.deposit, x.won), to_manwon(x.rent, x.won), x.target, x.note, x.lease_type); con.commit(); print('ok')
