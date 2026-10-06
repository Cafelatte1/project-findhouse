# 서울 청년 월세 임대 공고 모니터 — 설계도

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

서울에 거주·입주를 희망하는 **청년**이 지원할 수 있는 **월세형** 공공·사회·청년안심 임대 공고를 정기적으로 모아,

1. 보증금·월세 상한에 맞는지 자동 판정하고  
2. 신규·변경·마감임박만 알리며  
3. 공고문 첨부에서 주택형·가격을 구조화해 두어, 사람/에이전트가 원문과 대조하기 쉽게 한다.

**하지 않는 것:** 외부 신청·전화·메일, 자격(소득·자산·청약통장) 최종 확정, 숫자 추정·날조.

**대상 사용자(설정으로 맞춤):** 서울 청년·월세형 공공·사회 임대에 관심 있는 사용자.  
첫 설치 시 에이전트가 **반드시 사용자에게 확인**할 것:

1. 보증금·월세 상한 (만원) — `config.json`의 `max_deposit_manwon` / `max_rent_manwon`  
2. 근소초과 허용폭 — `near_tolerance_*`  
3. 제외할 자치구 — `exclude_gu` (예: 일부 고가 권역을 넣을지 여부)  
4. 알림·실행 시각 (권장 예시: 매일 12:30·18:30 KST)

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
[hdb.judge]   주택형 옵션 중 최선으로 match / near / …
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
| 알림 문구 | `report.py` | 카드형 마크다운 |
| 진입 | `run_daily.py` | 위 파이프라인 + 알림 상태(`notified_fit`) 갱신 |
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

**의도적으로 빼거나 보류:** 마이홈포털(일부 환경 TLS 차단 → data.go.kr API 보류), 청약홈 일반 민간임대, 자치구별 게시판, 청년몽땅(보조금 안내), 서울 사회주택 플랫폼(모집 게시판 부재).

관련성 키워드·제외어는 `collect.py`의 `RELEVANT` / `EXCLUDE` 정규식. 신혼·고령·전세형 등은 기본적으로 제외.

---

## 4. DB 스키마 요약

SQLite `housing.db`. 같은 `collected_date` 재실행은 `listings` upsert로 멱등.

| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `runs` | 실행 1회 | `collected_date`, `counts_json`, `errors_json`, 기준 금액 |
| `items` | 공고 영구 레지스트리 | `first_seen`/`last_seen`, `notified_fit`, `dedup_key` |
| `item_meta` | 사람이 확인한 단지·일정·자격 메모 | `name`, `gu`, `station`, `apply_*`, `eligibility` |
| `units` | 주택형·보증금비율 옵션 1행 = 1레코드 (만원) | `origin`=`human`\|`auto`\|`ocr`, `verified`, `doc_id` |
| `documents` | 첨부 처리 이력·캐시 | `sha256`, **`parser_version`**, `structured_json`, `kind`, `status` |
| `listings` | 일별 스냅샷·판정 | `fit`, `fit_basis`, `is_new`/`is_changed`/`is_due_soon`, 대표 면적·가격 |

**판정 근거 선택 (`pick_units`):** `human` 값이 하나라도 있으면 그것만 → 없으면 `auto` → 없으면 `ocr`.  
**fit 값:** `match` / `near` / `no` / `unknown` / `unverified`(OCR 잠정) / `closed` / `closed_match` / `excluded_region` / `irrelevant`.

`units.source`는 **수집처**(sh/soco/…)이고, 추출 출처는 **`origin`** 컬럼이다.

---

## 5. 첨부 문서 처리

대상: 관련·미마감이며 `sh` / `soco` / `socialhousing` 인 공고. 실행당 최대 N건(`docs.max_items_per_run`). 신청서·서약서 등 양식 파일명은 건너뜀. zip 미지원.

### 5.1 텍스트 PDF
1. `pdftotext`로 ‘보증금’+‘임대료/월세/사용료’가 있는 페이지만 선정  
2. **pdfplumber**로 표 추출 → 주택형·면적·보증금·월세·공급·비율/전환 옵션  
3. 해당 페이지만 **PNG 렌더**(`pdftoppm`) → `page` / `bbox` / `page_image` 를 유닛에 기록  
4. `documents.structured_json`에 메타 휴리스틱 + units 배열 저장 → `origin=auto`

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
- 키 = **`sha256` + `parser_version`** (`docs.PARSER_VERSION`, 현재 `2.0`)  
- 같은 공고·현재 버전이면 재다운로드·재파싱 없음  
- 다른 공고와 해시·버전이 같으면 `dup_hash`로 `structured_json`·units 복사  
- 파서/스키마를 바꾸면 `PARSER_VERSION`만 올려 무효화

`structured_json` 개요:

```text
parser_version, kind, method,
meta { name, address, apply_start, apply_end, eligibility },
units [{ unit_label, target, area_m2, deposit, rent, supply, option,
         page, table_index, bbox, page_image, note }],
pages_rendered[], crops[], text_excerpt
```

---

## 6. 설정값 (`config.json`)

| 키 | 기본(예시) | 의미 |
|---|---|---|
| `max_deposit_manwon` / `max_rent_manwon` | (사용자 설정 · 예시 3000 / 50) | 부합 상한. 옵션 중 **하나라도** 둘 다 만족하면 match |
| `near_tolerance_deposit_manwon` / `near_tolerance_rent_manwon` | (사용자 설정 · 예시 3000 / 10) | 근소초과 허용폭 |
| `exclude_gu` | (사용자 설정 · 예시 일부 고가 권역) | 지역 제외. 빈 배열이면 제외 없음 |
| `due_soon_days` | 3 | 마감임박 창 |
| `soco_assumed_window_days` | 3 | 청년안심 목록에 종료일 없을 때 가정 |
| `pages.sh` / `.soco` / … / `lh_lookback_days` | 5 / 2 / … / 60 | 수집 깊이 |
| `socialhousing_max_age_days` | 60 | 협회 ‘모집중’ 노후 게시 제외 |
| `docs.enabled` / `max_items_per_run` / `max_files_per_item` | true / 20 / 4 | 첨부 처리량 |
| `http_timeout_sec` / `http_retries` | 40 / 2 | 네트워크 |

CLI로 당일만 덮어쓰기: `--max-deposit`, `--max-rent` (만원).

---

## 7. CLI

```bash
cd project-findhouse   # 저장소 루트

# 일일 루틴
venv/bin/python run_daily.py
venv/bin/python run_daily.py --no-mark          # 알림상태 미갱신(시험)
venv/bin/python run_daily.py --summary          # 오늘 match/near 전체 포함
venv/bin/python run_daily.py --max-deposit <사용자보증금상한> --max-rent <사용자월세상한>

# 조회·첨부·기록
venv/bin/python query.py --date YYYY-MM-DD --pretty [--fit match,near]
venv/bin/python query.py --docs
venv/bin/python docs.py process <source> <id> [--force]
venv/bin/python units.py meta|unit|list|verify …
venv/bin/python sources.py sh|soco|lh|…
venv/bin/python selftest.py
```

가상환경 `venv/`: playwright, pdfplumber, rhwp-python, pyhwp 등. SOCO는 Chrome 필요.

---

## 8. 루틴 역할 분담

| 주체 | 하는 일 | 하지 않는 일 |
|---|---|---|
| **스크립트** (`run_daily.py`) | 수집, 첨부 텍스트/표 파싱, 캐시, 판정, DB, 마크다운 생성, `notified_fit` | 채팅 전송, 신청, OCR 숫자를 확정 금액으로 채택 |
| **스케줄러** | 매일 12:30·18:30 KST에 스크립트 실행 | — |
| **에이전트** | 출력이 ‘알릴 것 없음’이 아니면 사용자에게 전달; `unverified`/가격미확인 시 **페이지·크롭 이미지 확인** 후 `units.py` 기록; auto match는 원문 표 대조 후 `verify` 권장 | 숫자 추정, 사용자 승인 없는 외부 연락 |
| **사용자** | 자격·예산 최종 판단, 신청 | — |

알림이 비어 있으면(**알릴 것 없음**) 채팅에 보내지 않는 것이 기본이다.

---

## 9. 채팅 알림 템플릿

스크립트 기본 출력은 `report.py` 카드형(섹션: 부합 / 근소초과 / 마감임박 / 미검증 / 미확인 / 장애).  
**채팅으로 전달할 때는** 아래처럼 짧게 줄이는 것을 권장한다. 공고 항목은 **`🏠`만** 쓰고, 상태만 `✅`(확인·부합) / `⚠️`(주의·근소·미검증·장애)로 구분한다.

```markdown
**서울 청년 월세 수집** · {수집일}
• 기준: 보증금 ≤{D}만 · 월세 ≤{R}만

✅ **조건 부합** ({n})

• 🏠 **[{단지명}]({url})**
  - {구} · {역} · {면적}㎡
  - 보증금 {보증금}만 / 월세 {월세}만 (전환 옵션이면 표기)
  - 접수 {기간} · 마감 임박이면 표기

⚠️ **근소 초과** ({n})

• 🏠 **[{단지명}]({url})** … 같은 구조
```

- 이모지는 섹션 제목(✅/⚠️)과 단지명 앞 🏠에만 쓴다.
- 필요할 때만 수집 장애·가격 미확인 섹션을 짧게 덧붙인다.
- 금액·면적은 DB·원문 확인값만. 알릴 것이 없으면 보내지 않는다.

---

## 10. 확장 포인트

1. **채널:** `sources.ALL`에 함수 추가 + `config.pages` + (첨부가 있으면) `docs.SUPPORTED`  
2. **판정 기준:** `config.json` / CLI. 자격 자동판정은 `item_meta.eligibility` + 사용자 프로필 입력 후 별도 모듈  
3. **파서:** `docs.PARSER_VERSION` 증가 → 캐시 무효. 표 파서·단위 추정은 `parse_table` / `to_manwon`  
4. **알림 전송:** `report.render_alert` 결과를 슬랙·메일 등으로 보내는 어댑터(승인 게이트 필수)  
5. **API:** 공공데이터포털(LH·마이홈 등) 키 연동 시 `sources`만 교체 가능  
6. **정책:** 보편형 공공임대 등 새 유형은 `RELEVANT` 키워드·자격 규칙만 추가

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
- 제외 지역은 `item_meta.gu` 기준이라, 다단지 공고는 단지별 구 필터가 약함.

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
| `sources.py` | SH / 청년안심 / LH / 서울주거포털 / 사회주택협회 목록 |
| `collect.py` | 중복 제거, 첨부 트리거, 판정, listings upsert |
| `docs.py` | PDF(pdfplumber+페이지 이미지) / HWP(rhwp) / 캐시 |
| `hdb.py` | SQLite + match/near 판정 |
| `report.py` | 알림 마크다운 (채팅용으로 ✅/⚠️+🏠 축약 가능) |
| `run_daily.py` | 하루 N회 호출하는 단일 진입점 |

### 13.4 첫 설정 체크리스트 (사용자에게 질문)
1. 보증금·월세 상한(만원)과 근소초과 여유  
2. 제외할 구(없으면 빈 목록)  
3. 알림을 받을 시각 — **예시** 매일 12:30·18:30 KST (하루 2회)  
4. (선택) 관심 주택 유형 키워드 추가 여부  

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
