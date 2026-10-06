# 서울 공공·사회 임대 공고 모니터 (월세·전세 × 청년·신혼부부) — 설계도

공유·복제·다른 에이전트 온보딩용 설계 문서. 금액·제외 구 등은 예시이며, 첫 설정 시 사용자에게 물어 채웁니다.

같이 보기: [`README.md`](README.md), [`config.example.json`](config.example.json).

| | |
|---|---|
| 위치 | 저장소 루트 (데이터 위치는 `HOUSING_DIR` 환경변수로 변경 가능, 기본 = 스크립트 폴더) |
| 진입점 | `venv/bin/python run_daily.py` |
| DB | `housing.db` (SQLite) |
| 설정 | `config.json` (금액 단위: **만원**) |
| 권장 주기 | 매일 12:30·18:30 KST |

---

## 1. 목적

서울의 공공·사회·청년안심 임대 공고를 정기적으로 모아, 사용자가 고른 **임대유형**(`lease_types`: 월세 `monthly` / 전세 `jeonse`)과
**대상**(`targets`: 청년 `youth` / 신혼부부 `newlywed` — 예비신혼·신생아가구 포함)에 맞춰

1. 유형별 상한(월세: 보증금·월세 / 전세: 전세보증금)에 맞는지 자동 판정하고  
2. 신규·변경·마감임박만 알리며  
3. 공고문 첨부에서 주택형·가격을 구조화해 두어, 사람/에이전트가 원문과 대조하기 쉽게 한다.

**하지 않는 것:** 외부 신청·전화·메일, 자격(소득·자산·청약통장·무주택·혼인기간) 최종 확정, 숫자 추정·날조.

**두 축과 하위호환:**

| 축 | 값 | 기본 | 효과 |
|---|---|---|---|
| 임대유형 `lease_types` | `monthly`, `jeonse` | `["monthly"]` | 유형마다 별도 판정(`fit_monthly`/`fit_jeonse`)·별도 알림 블록. 전세 선택 시 장기전세·든든전세·전세형 공고와 HUG 든든전세 수집 |
| 대상 `targets` | `youth`, `newlywed` | `["youth"]` | **선택한 대상만 표시.** 다른 대상 전용 공고(제목)·주택형 행(공급대상 열)은 완전히 제외. 고령자·수급자 행(`기타`)은 항상 제외 |
| 지원 프로그램 `support_programs` | true/false | 전세 선택 시 켬 | 전세임대(입주자가 집을 구해오는 지원형)를 📋 섹션에 공고 단위로만(가격 판정 없음) |

새 키가 없는 기존 `config.json`은 **월세+청년만**으로 동작하며 수집·관련성·판정·카드형 출력이 기존과 동일하다(selftest·실측 비교로 고정).

**대상 사용자(설정으로 맞춤):** 서울 공공·사회 임대에 관심 있는 청년·(예비)신혼부부.  
첫 설치 시 에이전트가 **반드시 사용자에게 확인**할 것은 §13.4 체크리스트(임대유형·대상·상한·제외 구·시각 등)다.

아래 표·예시의 금액·제외 구는 **샘플일 뿐**이며, 운영본 `config.json`과 무관한 공유용 예시입니다. 혼인·주택소유·직업·생년 등 개인 프로필은 이 설계도에 넣지 않습니다(공고별 자격은 사용자가 원문에서 확인).

---

## 2. 아키텍처

```
[sources.py]  채널별 목록 수집 (curl / playwright)
      ↓
[collect.py]  정규화·중복제거·관련성 필터
      ↓
[docs.py]     신규·미가격 공고 첨부 다운로드 → 파싱 → documents/units
      ↓
[hdb.judge / judge_jeonse]  유형별(월세/전세) 주택형 옵션 중 최선으로 match / near / …
      ↓
[housing.db]  runs + listings 스냅샷 (같은 날 upsert)
      ↓
[report.py]   마크다운 알림 (알릴 것만 / 없으면 조용히)
```

| 단계 | 모듈 | 책임 |
|---|---|---|
| 수집 | `sources.py` | 채널별 독립 수집. 한 채널 실패해도 나머지는 진행 |
| 오케스트레이션 | `collect.py` | 중복·재게시 승계, 첨부 트리거, 판정, DB 기록 |
| 첨부 | `docs.py` | PDF/HWP/이미지 처리, sha256 캐시, `structured_json` |
| 스키마·판정 | `hdb.py` | SQLite DDL, `judge` / `pick_units` |
| 알림 문구 | `report.py` | 채팅 블록형(`render_chat`, 기본) / 카드형(`render_alert`, `--format cards`) |
| 진입 | `run_daily.py` | 위 파이프라인 + 알림 상태(`notified_fit`/`notified_jeonse`/`notified_support`) 갱신 |
| 수동 확정 | `units.py` | 원문 확인값을 `origin=human` 으로 기록 / `verify` 승격 |

데이터 흐름은 **목록 → (필요 시) 첨부 → 판정 → 스냅샷 → 알림** 한 방향이다. 외부 API로 신청·전송하지 않는다.

---

## 3. 수집 채널

| 키 | 채널 | 방법 | 비고 |
|---|---|---|---|
| `sh` | SH 인터넷청약 주택임대 게시판 | curl | 핵심. 사회주택·특화형 매입 포함 |
| `soco` | 서울시 청년안심주택 게시판 | **playwright** (headless Chrome) | 핵심. 민간·공공 청년안심 |
| `lh` | LH청약플러스 (서울, N일) | curl POST | 행복·매입 등. 0건은 구조변경 카나리로 장애 구분 |
| `seoulportal` | 서울주거포털 공공임대 | curl | SH 미러·상태값. 저비용 백업 |
| `socialhousing` | 한국사회주택협회 ‘입주자 모집중’ | curl | SH 밖 사회주택 보완. 오래된 ‘모집중’은 제외 |
| `hug` | HUG 든든전세주택(안심전세포털) | curl(cp949 목록) + 공고 PDF `pdftotext` | **전세 선택 시만.** 공고 단위: 서울 공급 호수·보증금 규칙(시세 90% 이하)·접수기간·자격. 주택별 가격은 수집 안 함(`NOTICE_LEVEL`) |
| `lh_support` | LH 전세임대(전국 수시) | curl POST (`uppAisTpCd=13`, `panNm=전세임대`) + 공고 상세 | **지원 프로그램 켬 시만.** 접수중·공고중 + **상세 공급 목록에 서울 공급이 있는 공고만**(비고 `서울 N호 · K개 구`, 제외 구 반영) |

`lh`는 `pages.lh_rolling_days`(새 설정 기본 365, 구 설정 0)만큼 과거 게시분 중 **지금 접수중·공고중**인 연중 수시모집을 보강한다.  

**LH 지역 판정 (`sources.lh_region_keep`, 2026-10-06 실측):** 목록의 지역 열은 **첫 지역 + ‘외’** 표기다. ‘외’는 *그 지역 외 다른 지역도 포함*이라는 뜻이지 ‘그 지역 제외’가 아니다
(예: ‘인천광역시 외’ = 인천·부천 공고, ‘대구광역시 외’ = 대구·경산 등). 전세임대 3건(청년 1순위·신혼·신생아 Ⅰ/Ⅱ)은 모두 ‘서울특별시 외’이며,
공고 상세(`selectWrtancInfo.do` POST) 안 공급 목록 JSON(지자체별 `sbdLgoNm`·`totRsdcSplQom`, 230행)에 **서울 25개 구 행**이 있다(청년 750/7,000호, 신혼·신생아Ⅰ 625/5,700호, Ⅱ 145/1,170호).
또 목록의 지역 필터(`cnpCd=11`)는 신뢰할 수 없다(서울 필터에 청년 전세임대만 나오고 신혼·신생아 Ⅰ/Ⅱ는 빠짐, 부산·경기 필터에도 청년 전세임대가 나옴).
규칙: 상세 공급 목록이 있으면 그것이 최종(서울 행 합계 > 0, 제외 구를 뺀 값 > 0일 때만 유지) → 없으면 지역 열에 ‘서울’ 포함·‘전국’은 유지, 그 밖의 지역(‘인천광역시 외’, 단일 타 지역)은 제외.
`lh`(서울 필터 결과)도 지역 열이 ‘서울특별시’가 아니면 같은 규칙으로 확인한다.
서울주거포털 목록 셀 안 HTML 주석(`<!-- 2021-01-25 클래스 수정 -->`)을 먼저 제거해 게시일을 바로 읽고, 링크 열의 SH 상세 URL을 공고 링크로 쓴다.

**의도적으로 빼거나 보류:** 마이홈포털(일부 환경 TLS 차단 → data.go.kr API 보류), 청약홈 일반 민간임대, 자치구별 게시판, 청년몽땅(보조금 안내), 서울 사회주택 플랫폼(모집 게시판 부재).

관련성(`collect.is_relevant(it, cfg)`)은 **제목·소스 카테고리만** 본다(본문 키워드 금지 — 월세 공고문에도 ‘전세대 전액 보증’ 같은 문구가 흔함).

| 정규식 | 역할 |
|---|---|
| `RELEVANT` | 월세형 공고 키워드(청년·행복주택·매입임대·사회주택…) |
| `NOISE` | 결과·안내 공지(당첨자·접수결과·경쟁률·계약안내…) — 항상 제외 |
| `ALWAYS_EX` | 대상 밖(다자녀·고령자·기숙사·예술인…) — 항상 제외 |
| `NEWLYWED_ONLY_EX` | 공공한옥(미리내집) — 신혼부부 선택 시만 허용 |
| `NEWLYWED` / `YOUTH` | 제목의 대상 표기(신혼·신생아·미리내집 / 청년·대학생) |
| `JEONSE` / `SUPPORT` | 임대유형 분류(장기전세·든든전세·전세형·전세주택 / 전세임대) → `lease_of()` |

규칙: 제목이 **한 대상 전용**(신혼·신생아만 / 청년만)이면 그 대상을 골랐을 때만 포함. **혼합 제목**(‘청년·신혼부부’, ‘청년 및 신혼부부’)과 대상 표기가 없는 일반 공고는
어느 대상을 골라도 포함하고, 주택형 행은 선택 대상만 판정한다. ‘청년안심주택’은 사업명이라 대상 표기로 보지 않는다.
**구 동작과 다른 점(의도적):** 구 버전은 청년만 골라도 혼합 제목 공고를 버렸다(신혼 키워드 제외). 지금은 포함한다. 2026-10-06 실측(라이브 설정 복사본, 107건)에서는 혼합 제목 공고가 없어 바뀐 판정 0건.
제목에 대상 표기가 없는 일반 공고는 주택형 표의 **공급대상 열**로 거른다(`target` = 청년/신혼부부/공통/기타).
월세 공고는 전세만 골라도 수집한다(🔁 전세에 가까운 옵션 탐색용).

---

## 4. DB 스키마 요약

SQLite `housing.db`. 같은 `collected_date` 재실행은 `listings` upsert로 멱등.

| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `runs` | 실행 1회 | `collected_date`, `counts_json`, `errors_json`, 기준 금액 |
| `items` | 공고 영구 레지스트리 | `first_seen`/`last_seen`, `notified_fit`(월세) / `notified_jeonse` / `notified_support`, `dedup_key` |
| `item_meta` | 사람이 확인한 단지·일정·자격 메모 | `name`, `gu`, `station`, `apply_*`, `eligibility` |
| `units` | 주택형·보증금비율 옵션 1행 = 1레코드 (만원) | `origin`=`human`\|`auto`\|`ocr`, `verified`, `doc_id`, `target`, **`lease_type`**(전세 행은 rent=0) |
| `documents` | 첨부 처리 이력·캐시 | `sha256`, **`parser_version`**, `structured_json`, `kind`, `status` |
| `listings` | 일별 스냅샷·판정 | `fit`(유형별 중 최선), `fit_monthly`/`reason_monthly`, `fit_jeonse`/`reason_jeonse`, `best_json`(유형별 대표 옵션·🔁 `conv`), `lease_type`, `targets`, `elig_note`, `extra_note`, `fit_basis`, `is_new`/`is_changed`/`is_due_soon`, 대표 면적·가격 |

**판정 근거 선택 (`pick_units`):** `human` 값이 하나라도 있으면 그것만 → 없으면 `auto` → 없으면 `ocr`.  
**fit 값:** `match` / `near` / `no` / `unknown` / `unverified`(OCR 잠정) / `program`(지원 프로그램, 가격 판정 없음) / `closed` / `closed_match` / `excluded_region` / `irrelevant`.  
새 컬럼은 `hdb.connect()`가 `ALTER TABLE ADD COLUMN`으로 멱등 추가한다(기존 DB 그대로 사용 가능).

**전세 판정 (`judge_jeonse`):** 후보 = 전세 표 행(rent=0) + **전세에 가까운 월세 옵션**(월세 ≤ `jeonse_like_max_rent_manwon`(10) **그리고** 보증금 ≥ `jeonse_like_min_deposit_manwon`(5000), 알림에 🔁 전환).
보증금 ≤ 상한 → match, ≤ 상한+근소폭 → near. 후보가 없으면 전세 공고는 `unknown`, 월세 공고는 해당 없음(전세 블록에 안 나옴).
최소 보증금 조건이 없으면 562만/6.63만 같은 저가 재개발임대 옵션이 ‘전세’로 잡히므로 기본 5000만을 둔다.
전월세 전환율(`conversion_rate_pct`)은 표시 전용이며 판정에 쓰지 않는다.

**제외 구는 주택형 단위로도 적용 (`hdb.unit_gu` / `drop_excluded_gu`):** 공고 단위(`item_meta.gu`) 확인 뒤, 주택형 행의 구(`units.gu` 또는 라벨·비고 속 `마포구`/`서울마포구` 표기)가 `exclude_gu`에 있으면
그 행을 월세·전세 판정과 대표 옵션 선택에서 뺀다. 모든 행이 제외 구면 `excluded_region`(알림 안 함). 구를 모르는 행은 유지한다.

`units.source`는 **수집처**(sh/soco/…)이고, 추출 출처는 **`origin`** 컬럼이다.

---

## 5. 첨부 문서 처리

대상: 관련·미마감이며 `sh` / `soco` / `socialhousing` 인 공고. 실행당 최대 N건(`docs.max_items_per_run`). 신청서·서약서 등 양식 파일명은 건너뜀. zip 미지원.

### 5.1 텍스트 PDF
1. `pdftotext`로 ‘보증금’+‘임대료/월세/사용료’가 있는 페이지만 선정 (전세 공고는 ‘전세(대 제외)’ 페이지도)  
2. **pdfplumber**로 표 추출 → 주택형·면적·보증금·월세·공급·비율/전환 옵션  
3. 해당 페이지만 **PNG 렌더**(`pdftoppm`) → `page` / `bbox` / `page_image` 를 유닛에 기록  
4. `documents.structured_json`에 메타 휴리스틱 + units 배열 저장 → `origin=auto`  
5. **보증금만 있는 표**는 전세 공고(제목 분류)이거나 헤더에 ‘전세’(‘전세대’ 제외)가 있을 때만 전세 행(rent=0, `lease_type=jeonse`)으로 받는다 — 월세 표 오판 방지  
6. **공급대상 열**(공급대상·입주대상·공급유형 등)을 읽어 `target` 지정: 청년+신혼 둘 다 → 공통, 고령자·수급자 → 기타(판정 제외)

### 5.2 HWP / HWPX
- **우선:** `rhwp-python` (`import rhwp`) IR 표 → 동일 표 파서  
- 환경에 따라 FreeType 선로드가 필요할 수 있음(번들 심볼 이슈)  
- **폴백:** `hwp5html`(HWP) / HWPX section XML  
- 결과 `origin=auto`

### 5.3 이미지형 PDF·이미지 — OCR을 판정에 쓰지 않음
- 기본(`docs.ocr=false`): 숫자를 추출하지 않고 **페이지 PNG**만 렌더해 `documents.status=needs_agent`로 남긴다 → 판정 `unknown`(🔎 가격 미확인) 알림에 PNG 경로 포함.  
- 선택(`docs.ocr=true` 또는 `docs.py process … --ocr`): tesseract 숫자 후보 + 표 영역 크롭. **`origin=ocr` → fit=`unverified`**, 최종 금액으로 채택하지 않는다.  
- **에이전트(또는 사람)가 페이지/크롭 이미지를 직접 보고** `units.py`로 `human` 값을 기록한다.

### 5.4 캐시
- 키 = **`sha256` + `parser_version`** (`docs.PARSER_VERSION`, 현재 `2.1`)  
- 같은 공고·현재 버전이면 재다운로드·재파싱 없음  
- 다른 공고와 해시·버전이 같으면 `dup_hash`로 `structured_json`·units 복사  
- 파서/스키마를 바꾸면 `PARSER_VERSION`만 올려 무효화

`structured_json` 개요:

```text
parser_version, kind, method,
meta { name, address, apply_start, apply_end, eligibility },
units [{ unit_label, target, area_m2, deposit, rent, lease_type, supply, option,
         page, table_index, bbox, page_image, note }],
pages_rendered[], crops[], text_excerpt
```

---

## 6. 설정값 (`config.json`)

| 키 | 기본(예시) | 의미 |
|---|---|---|
| `lease_types` | `["monthly"]` | 임대유형: `monthly` / `jeonse` / 둘 다 |
| `targets` | `["youth"]` | 대상: `youth` / `newlywed` / 둘 다. 선택 대상만 표시 |
| `max_deposit_manwon` / `max_rent_manwon` | (사용자 설정 · 예시 3000 / 50) | 월세 부합 상한. 옵션 중 **하나라도** 둘 다 만족하면 match (`monthly: {...}` 블록으로도 가능) |
| `near_tolerance_deposit_manwon` / `near_tolerance_rent_manwon` | (사용자 설정 · 예시 3000 / 10) | 월세 근소초과 허용폭 |
| `jeonse.max_deposit_manwon` / `jeonse.near_tolerance_deposit_manwon` | 20000 / 5000 | 전세 상한(2억)·근소폭 |
| `jeonse.jeonse_like_max_rent_manwon` / `jeonse.jeonse_like_min_deposit_manwon` | 10 / 5000 | 🔁 전환 옵션 기준(월세 ≤10만 **그리고** 보증금 ≥5000만) |
| `support_programs` | null(=전세 선택 시 켬) | 전세임대 지원 프로그램 📋 섹션 |
| `conversion_rate_pct` | null | 표시 전용 전환율 |
| `pages.lh_rolling_days` / `pages.hug` | 365(구 설정 0) / 1 | LH 수시모집 보강 기간 / HUG 공고 수 |
| `exclude_gu` | (사용자 설정 · 예시 일부 고가 권역) | 지역 제외. 빈 배열이면 제외 없음 |
| `due_soon_days` | 3 | 마감임박 창 |
| `soco_assumed_window_days` | 3 | 청년안심 목록에 종료일 없을 때 가정 |
| `pages.sh` / `.soco` / … / `lh_lookback_days` | 5 / 2 / … / 60 | 수집 깊이 |
| `socialhousing_max_age_days` | 60 | 협회 ‘모집중’ 노후 게시 제외 |
| `docs.enabled` / `max_items_per_run` / `max_files_per_item` | true / 20 / 4 | 첨부 처리량 |
| `http_timeout_sec` / `http_retries` | 40 / 2 | 네트워크 |

CLI로 당일만 덮어쓰기: `--max-deposit`, `--max-rent`(월세), `--jeonse-max-deposit`(전세), `--types monthly,jeonse`, `--targets youth,newlywed`.

---

## 7. CLI

```bash
cd project-findhouse   # 저장소 루트

# 일일 루틴
venv/bin/python run_daily.py
venv/bin/python run_daily.py --no-mark          # 알림상태 미갱신(시험)
venv/bin/python run_daily.py --summary          # 오늘 match/near 전체 포함
venv/bin/python run_daily.py --max-deposit <사용자보증금상한> --max-rent <사용자월세상한>
venv/bin/python run_daily.py --types monthly,jeonse --targets youth,newlywed --jeonse-max-deposit 18000
venv/bin/python run_daily.py --format cards     # 레거시 카드형

# 조회·첨부·기록
venv/bin/python query.py --date YYYY-MM-DD --pretty [--fit match,near] [--type monthly|jeonse|support] [--target 신혼부부]
venv/bin/python query.py --docs
venv/bin/python docs.py process <source> <id> [--force]
venv/bin/python units.py meta|unit|list|verify …       # 전세 행: unit … --deposit 18410 --lease-type jeonse (--rent 생략)
venv/bin/python sources.py sh|soco|lh|…
venv/bin/python selftest.py
```

가상환경 `venv/`: playwright, pdfplumber, rhwp-python, pyhwp 등. SOCO는 Chrome 필요.

---

## 8. 루틴 역할 분담

| 주체 | 하는 일 | 하지 않는 일 |
|---|---|---|
| **스크립트** (`run_daily.py`) | 수집, 첨부 텍스트/표 파싱, 캐시, 판정, DB, 마크다운 생성, `notified_*` | 채팅 전송, 신청, OCR 숫자를 확정 금액으로 채택 |
| **스케줄러** | 매일 12:30·18:30 KST에 스크립트 실행 | — |
| **에이전트** | 출력이 ‘알릴 것 없음’이 아니면 사용자에게 전달; `unverified`/가격미확인 시 **페이지·크롭 이미지 확인** 후 `units.py` 기록; auto match는 원문 표 대조 후 `verify` 권장 | 숫자 추정, 사용자 승인 없는 외부 연락 |
| **사용자** | 자격·예산 최종 판단, 신청 | — |

알림이 비어 있으면(**알릴 것 없음**) 채팅에 보내지 않는 것이 기본이다.

---

## 9. 채팅 알림 템플릿

`run_daily.py` 기본 출력(`--format chat`, `report.render_chat`)이 곧 채팅용 문구다. 카드형은 `--format cards`.

구성: **제목 → 한 줄 요약 → 임대유형별 블록(월세 → 전세, 선택했고 내용이 있을 때만) → 📋 지원 프로그램 → ⚠️ 수집 장애.**  
블록마다 자체 기준줄과 ✅ 조건 부합 / ⚠️ 근소 초과 섹션(필요 시 ⚠️ 마감 임박 / 공고 단위 확인 / 가격 미확인 / OCR 미검증)을 둔다.

```markdown
**서울 {대상} {임대유형} 수집** · {수집일}
월세 부합 {n} · 근소 {n} / 전세 부합 {n}

**월세**
• 기준: 보증금 ≤{D}만 · 월세 ≤{R}만

✅ **조건 부합** ({n})

• 🏠 **[{단지명 또는 사업명}]({url})** · {대상: 대상 둘 다 선택 시만}
  - {여러 단지 사업이면 최적 주택형의 단지명} · {구} · {역} · {면적}㎡
  - 보증금 {보증금}만 / 월세 {월세}만 (전환 옵션) · 자동추출
  - 접수 {MM-DD ~ MM-DD} · 마감 임박 · 무주택세대 자격

⚠️ **근소 초과** ({n})

• 🏠 … 같은 구조

**전세**
• 기준: 전세 보증금 ≤{J}

✅ **조건 부합** ({n})

• 🏠 **[청계로벤하임](url)**
  - 동묘앞역 · 20㎡
  - 🔁 전환 · 보증금 1억 8,410만 / 월세 3.24만 · 자동추출   ← 전세에 가까운 월세 옵션 (전세 행이면 `전세 1억 8,410만`)
  - 접수 10-06 ~ 10-09 · 마감 임박

📋 **지원 프로그램** ({n})

• [{공고명}]({url})
  - 접수 ~ {마감} · 지역 …
  - 집을 직접 구해 신청 · 가격 판정 없음
```

**항목 이름·줄 규칙 (`report.notice_name` / `unit_place` / `_item`):**
- 링크 텍스트 = 단지명: 사람 기록(`item_meta.name`) > 제목 정리(앞쪽 `[민간임대]`·`[서울지역본부]`·`(수정)` 등, 날짜, `20xx년`·하반기·`N차`, ‘입주자 모집공고’·‘추가모집공고’·‘잔여세대’·‘N순위’, 끝의 호실번호 제거; `동묘앞역 청계로벤하임` → 역 분리, `(금천구)` → 구 분리; `…주택` → `매입임대`·`재개발임대` 등 축약).
- 여러 단지를 묶는 사업 공고(제목에 임대·장기전세·든든전세·행복주택·미리내집 등)는 `SH 재개발임대 일반모집`·`LH 청년 전세임대` 처럼 기관+사업명, 같은 사업명이 겹치면 `(운영기관)`을 붙이고, **첫 하위 줄에 최적 주택형 라벨에서 뽑은 단지명**(+ 라벨의 구·역)을 쓴다.
- 하위 줄은 최대 3줄: ① 단지명(사업 공고)·구·역·면적 ② 가격(+옵션, 자동추출이면 `· 자동추출`) — 가격이 없는 공고 단위(HUG)는 비고 ③ 접수기간(올해면 MM-DD)·마감 임박·자격(무주택세대 기준이면 `무주택세대 자격`). **모르는 값은 자리표시 없이 생략**하고, 셋 다 비면 줄 자체를 뺀다. (이미지형 미확인 공고만 에이전트용 `이미지:` 줄이 추가될 수 있음.)

- 이모지는 섹션 제목(✅/⚠️), 단지명 앞 🏠, 전세 전환 옵션 🔁, 지원 프로그램 📋에만 쓴다.
- 대상이 하나면 대상 태그를 붙이지 않고, 임대유형이 하나면 블록 제목(**월세**/**전세**)을 생략한다 → 기존 승인 템플릿(`**서울 청년 월세 수집**`)과 같다.
- 대상 태그는 판정된 주택형의 공급대상(청년/신혼부부)이 우선, 없으면 공고 제목 표기.
- 🔁 옵션은 조건을 만족하는 블록 어디에나 나온다(월세 상한도 맞으면 월세 블록에도).
- 금액: 1억 이상은 `1억 8,410만`, 그 아래는 `2,000만`. 금액·면적은 DB·원문 확인값만.
- 알림 상태는 유형별로 따로 기록(월세 `notified_fit` 태그 `@D/R[/대상]` — 기존 값과 호환, 전세 `notified_jeonse` `@J[/대상]`, 지원 `notified_support`). 지원 프로그램은 새 공고·마감 임박일 때만.
- 알릴 것이 없으면 `**알릴 것 없음**` 한 줄 → 보내지 않는다.

---

## 10. 확장 포인트

1. **채널:** `sources.ALL`에 함수 추가 + `config.pages` + (첨부가 있으면) `docs.SUPPORTED`  
2. **판정 기준:** `config.json` / CLI. 자격 자동판정은 `item_meta.eligibility` + 사용자 프로필 입력 후 별도 모듈  
3. **파서:** `docs.PARSER_VERSION` 증가 → 캐시 무효. 표 파서·단위 추정은 `parse_table` / `to_manwon`  
4. **알림 전송:** `report.render_alert` 결과를 슬랙·메일 등으로 보내는 어댑터(승인 게이트 필수)  
5. **API:** 공공데이터포털(LH·마이홈 등) 키 연동 시 `sources`만 교체 가능  
6. **정책:** 보편형 공공임대 등 새 유형은 `RELEVANT` 키워드·자격 규칙만 추가  
7. **임대유형·대상:** 새 대상은 `hdb.TARGET_LABEL` + `collect` 대상 정규식 + `docs` 공급대상 매핑, 새 임대유형은 `lease_of` + 판정 함수 + `report` 블록

---

## 11. 한계

- 텍스트 PDF·HWP 표 추출은 레이아웃에 민감. 다단지·추가모집(일부 호실만)은 표의 다른 행이 섞일 수 있음 → 라벨·원문 확인.  
- 메타(단지명·접수기간) 휴리스틱은 자주 비어 있음. 일정은 목록·`item_meta`가 우선.  
- **OCR 숫자는 신뢰하지 않음.** 이미지 공고는 에이전트가 PNG를 본다.  
- 청년안심 목록은 신청 시작일만 주는 경우가 많아 종료일을 가정(+N일).  
- 서울주거포털 ‘모집중’은 발표 전까지 유지 → 접수기간으로 쓰지 않음.  
- 사회주택협회 ‘모집중’도 갱신이 늦음 → 게시 경과일 필터.  
- 마이홈 등 일부 사이트는 환경에 따라 TLS/차단.  
- zip 첨부, LH 공고 PDF 일괄 파싱은 미구현.  
- 주택형 단위 제외 구는 라벨·비고에 구 표기가 있을 때만 동작한다(재개발임대 단지명만 있는 행 등은 구를 몰라 유지).  
- 알림 단지명은 제목·자동추출 라벨 휴리스틱이라 어색할 수 있다 → `units.py meta --name`으로 사람 기록 시 그 값을 쓴다.  
- HUG 든든전세·LH 전세임대는 공고 단위만(주택별 가격 없음). HUG 공고문은 접수 마감 표기가 서로 다를 수 있어(예: 10.8 / 10.12) 늦은 날짜를 저장하고 경고를 붙인다.  
- 자격 한 줄(`elig_note`)은 사람 기록 > 원문 문구(HUG) > ‘세대 기준 공고 — 원문 확인’ 일반 안내 순. 소득·자산·혼인기간은 판정하지 않는다.

---

## 12. 파일 지도

| 경로 | 역할 |
|---|---|
| `DESIGN.md` | 본 설계도 (공유 템플릿) |
| `README.md` | 개요·빠른 시작 |
| `requirements.txt` / `config.example.json` / `.gitignore` | 의존성·설정 예시·로컬 데이터 제외 |
| `run_daily.py` / `collect.py` / `sources.py` | 루틴 파이프라인 |
| `docs.py` / `hdb.py` / `report.py` / `units.py` / `query.py` | 첨부·DB·알림·기록·조회 |
| `config.json` / `housing.db` / `docs/` | (로컬 생성·git 제외) 설정·상태·첨부 원본·페이지 이미지 |
| `selftest.py` | 오프라인 회귀 |
| `templates/alert.md.j2` | 알림 구조 참고용 스케치 |

복제 시: 이 디렉터리 + `venv` 의존성 + (SOCO용) Chrome, `config.json`의 예산·제외 구만 환경에 맞게 바꾸면 된다. 개인 식별 정보·실신청은 저장·자동화하지 말 것.

---

## 13. 처음 설치하는 에이전트를 위한 재구현 가이드

운영 중인 타인의 `housing.db`·`config.json`을 그대로 쓰지 말고, **이 저장소를 clone**해 새 작업 폴더에서 `config.json`을 직접 만드세요.

### 13.1 필요한 환경·패키지
- Python 3.10+ 가상환경 (`venv`)
- `pdfplumber` — 텍스트 PDF 표 추출
- `rhwp-python` (`import rhwp`) — HWP/HWPX (환경에 따라 시스템 FreeType 선로드 필요할 수 있음). 폴백: `pyhwp`/`hwp5html`
- `playwright` + headless Chrome — 청년안심(soco) 목록 JS 렌더
- 시스템 도구: `curl`, `pdftotext`/`pdftoppm`(poppler), (선택) `tesseract` — **OCR 숫자는 판정에 쓰지 않음**, 이미지 공고는 에이전트가 PNG를 직접 확인
- 선택: Pillow

### 13.2 권장 폴더 구조
```text
housing/
  config.json          ← config.example.json 복사 후 사용자 답으로 채움
  housing.db           ← 실행 시 자동 생성
  run_daily.py         # 진입점
  collect.py           # 수집 오케스트레이션·판정·DB
  sources.py           # 채널별 목록
  docs.py              # 첨부 파싱·캐시(sha256+parser_version)
  hdb.py               # 스키마·judge
  report.py            # 마크다운 알림
  units.py / query.py  # 원문 확인 기록·조회
  docs/                # 첨부 원본·페이지 PNG
  selftest.py
```

### 13.3 모듈 역할 (한 줄)
| 모듈 | 역할 |
|---|---|
| `sources.py` | SH / 청년안심 / LH / 서울주거포털 / 사회주택협회 / HUG 든든전세 / LH 전세임대 목록 |
| `collect.py` | 중복 제거, 첨부 트리거, 판정, listings upsert |
| `docs.py` | PDF(pdfplumber+페이지 이미지) / HWP(rhwp) / 캐시 |
| `hdb.py` | SQLite + 설정 정규화 + 월세/전세 match/near 판정 |
| `report.py` | 채팅 블록형 알림(기본) / 카드형 |
| `run_daily.py` | 하루 N회 호출하는 단일 진입점 |

### 13.4 첫 설정 체크리스트 (사용자에게 질문)
1. 임대유형 — 월세 / 전세 / 둘 다 (`lease_types`)  
2. 대상 — 청년 / 신혼부부(예비·신생아 포함) / 둘 다 (`targets`) — 고른 대상만 알림에 나온다  
3. (월세 선택 시) 보증금·월세 상한(만원)과 근소초과 여유  
4. (전세 선택 시) 전세보증금 상한과 근소초과 여유(기본 2억 / +5000만), 🔁 전환 옵션 기준(기본 월세 ≤10만·보증금 ≥5000만)  
5. 전세임대 같은 **지원 프로그램**(집을 직접 구해 신청)도 알릴지 (`support_programs`, 전세 선택 시 기본 켬)  
6. 제외할 구(없으면 빈 목록)  
7. 알림을 받을 시각 — **예시** 매일 12:30·18:30 KST (하루 2회)  
8. (선택) 전월세 전환율 표시 여부(`conversion_rate_pct`, 표시 전용)  
9. (선택) 관심 주택 유형 키워드 추가 여부  

답을 `config.json`에 반영한 뒤 `venv/bin/python selftest.py` → `run_daily.py --no-mark`로 시험.

### 13.5 루틴 등록 예시 (하루 2회)
```cron
# 사용자 로컬 TZ가 KST가 아니면 시각을 맞출 것
30 12 * * * cd /path/to/project-findhouse && venv/bin/python run_daily.py >> logs/daily.log 2>&1
30 18 * * * cd /path/to/project-findhouse && venv/bin/python run_daily.py >> logs/daily.log 2>&1
```
에이전트 루틴: 스크립트 출력이 「알릴 것 없음」이 아니면 채팅으로 전달. 이미지·가격미확인은 페이지 PNG를 보고 `units.py`로 기록.

### 13.6 채팅 템플릿 (이모지 최소화)
§9 템플릿과 동일하게 사용.

### 13.7 하지 말 것
- 사용자 생년·혼인·직업·실명 등 개인 프로필을 코드·설정·공유 문서에 하드코딩  
- OCR 숫자로 최종 금액 확정  
- 사용자 승인 없는 외부 신청·연락  
