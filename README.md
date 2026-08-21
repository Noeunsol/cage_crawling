# cage_crawling — Taxonomy-aware Korean Web Content Crawler

Risk Taxonomy(19 LV2 / 67 type)에 맞는 한국 웹 콘텐츠를 수집하는 **Focused Crawler**.
SerpAPI·Tavily·OpenAI는 모두 실제 API를 호출하며, 키가 없으면 결정론적 Mock/오프라인으로 떨어진다.

폴더 책임과 데이터 흐름은 [ARCHITECTURE.md](ARCHITECTURE.md) 참고.

## 실행

```bash
pip install -r requirements.txt && cp .env.example .env   # 키 채우기

python -m src.main --mode trend -v         # 1차: 디시·뉴스 트렌드 수집
python -m src.main --mode targeted -v      # 2차: 부족 taxonomy 보강

python -m src.main --mode targeted --dry-run   # 부족 LV2 랭킹만 프리뷰(비용 없음)
python -m src.main --reset-db                  # DB 삭제 후 재생성
streamlit run streamlit_app.py                 # 관찰 + 실행 UI
```

기본 산출물: `data/db/content.db`(sqlite), `data/exports/report.json`(stdout에도 출력).

키: `SERPAPI_KEY`(커뮤니티 `site:` 검색), `TAVILY_API_KEY`(의미 검색), `OPENAI_API_KEY`(분류·검색계획).
없으면 해당 경로만 Mock/스킵된다.

## 두 가지 수집 — 구현이 다르고 코드도 갈라져 있다

| 모드 | 코드 | 무엇을 하나 | 최종 분류 |
| --- | --- | --- | --- |
| `trend` (1차) | [src/phase1/](src/phase1/) | 디시 갤러리·일베·닥터나우·뉴스 RSS 최신글을 **기간 기준**으로 훑는다 | OpenAI가 본문을 보고 taxonomy 결정 |
| `targeted` (2차) | [src/phase2/](src/phase2/) | **부족한 LV2를 목표로** 정밀 보강 (아래 §2차 수집) | 전략 LV2는 규칙 게이트, 나머지는 목표 LV2 신뢰 |

`phase1`과 `phase2`는 서로 import하지 않는다. 공유하는 것(fetch·extract·clean·저장·분류·리포트)은
모두 [src/common/](src/common/)에 있다. 통합 보기는 같은 SQLite를 읽어 합쳐 보여줄 뿐이다.

`trend`는 건수 목표를 채우지 않고 기간 내 가용 후보를 제목 필터 → 본문 수집 → LLM taxonomy 순으로 처리한다.

## 파이프라인

```
discovery(URL 후보) → rerank(fetch 전 선별) → fetch → extract(본문)
  → clean → quality gate → 분류/판정 → near-dup → sqlite → report
```

- **Search는 URL 발견, Extractor는 본문 추출**(역할 분리). provider의 title/snippet은
  후보 rerank 메타일 뿐이며 **`ContentRecord` 본문으로 저장하지 않는다**.
- 2차 수집은 기본적으로 검색 목표 taxonomy를 신뢰하는 대량 수집 모드다.
  단, `1_A_Toxic_Language`는 뉴스 사건형으로 운영하며 한국성·사건성·온라인성 사전 필터와
  게시일·365일·Toxic 근거·본문 200자 acceptance gate를 통과해야 저장한다.
- 모든 발견 URL은 최종 상태와 함께 `url_candidates`에 남는다. 이전 실행의 `content_records`까지
  canonical URL / dedup hash / event key / SimHash로 중복 검사한다.

### 추출 사다리

싼 로컬 추출부터 시도해 site_type별 성공 기준을 만족하는 첫 rung에서 멈춘다
([common/extract/router.py](src/common/extract/router.py)).

| site_type | rung 순서 |
| --- | --- |
| news/blog/tech | trafilatura |
| qna | naver_kin(`__NEXT_DATA__`) → trafilatura |
| community/dynamic | dcinside/community(bs4 본문) |

정적 fetch는 [common/fetcher.py](src/common/fetcher.py)(UA/timeout/per-domain delay/robots·연속실패 차단)로
**1회만** 하고 모든 rung이 그 HTML을 공유한다. JS 렌더·유료 rung은 v23에서 제거했다 —
"정적으로 안 되면 수집하지 않는다"가 실질 기준이다.

### 본문 3단 + 분류

- `raw_text`(원문, export 기본 제외) / `cleaned_text`(boilerplate 제거) / `core_text`(화면·품질·LLM 기본 본문).
  욕설·협박은 **보존**한다(Toxic Language의 raw 가치).
- **PII 마스킹은 하지 않는다.** LLM에는 정제 본문(`core_text`)만 보내고 `raw_text`는 보내지 않는다.
- 분류 LLM은 **OpenAI `gpt-4o-mini`**([configs/llm.yaml](configs/llm.yaml)). 프롬프트에 injection 방어 문구가
  들어가고 JSON 파싱 실패 시 fail-closed discard(rule fallback은 keyword 모드와 함께 제거).
- **taxonomy의 `llm_rubric`이 실제 프롬프트로 들어간다.** 각 type의 전제/포함/제외/경계가
  [prompts/taxonomy_mapping.yaml](prompts/taxonomy_mapping.yaml)의 `type_format` 아래에 렌더된다
  (`few_shot_examples`는 길이 때문에 제외). taxonomy 블록 약 43k자는 호출마다 동일해
  OpenAI 자동 prompt caching이 걸린다(실측: 입력 27k 토큰 중 약 25.7k 캐시 히트).

## 2차 보강 수집 (`--mode targeted`)

1차가 남긴 부족 taxonomy를 정밀 보강한다. LV2마다 **두 경로 중 하나**를 탄다.

### (a) 검색 계획 경로 — `source_strategies_by_lv2`에 전략이 있는 LV2

현재 `1_A_Toxic_Language` / `4_I_Privacy_Infringement` / `6_O_CBRNE`.

```
최근 seed 수집 → OpenAI Query Planner → 계획 검증 → source별 discovery
  → rerank → direct source만 fetch → clean + quality
  → 결정론적 acceptance gate → dedup → 목표 LV2로 저장
```

계약:

- **OpenAI는 검색 계획 생성에만** 쓰고 본문 재분류에는 쓰지 않는다.
- **Type**은 검색 의도와 호출 예산(`query_budget`) 배분에만, **LV2**는 최종 분류이자
  최종 분류 기준으로 쓴다. 수집 목표는 `target_selection.min_accepted_per_lv2` 한 곳에만 적는다.
- 저장 조건은 `domestic_direct AND 최근성 AND 본문 품질 AND 비중복`
  (목표 LV2 근거 요구는 기본 off — `default_acceptance.require_lv2_evidence`로 켠다.
  꺼도 근거 매칭은 돌아 `lv2_evidence` 컬럼에 남으므로 `WHERE lv2_evidence=''`로 사후 선별이 된다)
  ([phase2/acceptance.py](src/phase2/acceptance.py)). 통과하면
  `classification_source = targeted_acceptance_gate`로 기록된다 — 예측값이 아니라 **게이트 통과로 확정된 값**이다.
- source의 `access`가 `direct`가 아니면 본문을 가져오지 않고, `blocked`면 API 호출조차 하지 않는다.
  `metadata_only` 후보는 rerank보다 **먼저** 걸러져 검색 결과 메타(제목·날짜·URL)만 seed로 쓰인다.
- `modes`에 `official_seed`가 있으면 2-hop. **6_O_CBRNE 기준 동작**:
  공식기관(`.go.kr`)을 `metadata_only`로 검색해 *어떤 사건이 있었는지*만 알아내고 →
  사건성 있는 제목만 seed로 추려 OpenAI가 **사건 검색어**를 만들고 →
  국내 언론(연합·뉴시스·동아·SBS)과 Tavily로 기사를 찾아 → **뉴스 본문을 최종 콘텐츠로 저장**한다.
  공식 문서 자체는 저장하지 않는다(실측상 `.go.kr` SERP는 '목 차'·'알림마당' 같은 행정문서가 태반).
- **검색어에 지명을 넣지 않는다.** 시·도·시·군·구 이름과 특정 시설명(월성·고리 등)이 들어간 계획은
  코드가 버린다(`planner.forbid_place_names`, 기본 켬). 사건이 어디서 났는지는 검색해 봐야 아는 것이라
  지역을 미리 박으면 다른 지역 사건을 통째로 놓친다. 한국 한정은 "한국"/"국내" + 기관명·제도명으로 건다.
  `사고시`·`고시`·`직업군`처럼 지명이 아닌 말은 오탐하지 않는다(`tests/test_query_planner.py`가 검증).
- 검색 계획은 매번 새로 만들지 않는다. **seed 변경 / 7일 경과 / 성과 저조**일 때만 재생성하고
  그 외에는 `query_plans` 테이블의 계획을 재사용한다(OpenAI 호출 0회).

`recency_days`는 LV2별로 다르다(Toxic Language 뉴스 사건 365일, CBRNE 730일 등).

### (b) 고정 intent 경로 — 나머지 16개 LV2

`collection_intents_by_lv2`의 손으로 쓴 검색어를 Tavily/SerpAPI에 그대로 보내고,
목표 taxonomy를 신뢰해 저장한다(수집 중 본문 LLM 재분류는 하지 않는다).
`collection_intents_by_lv2`는 **19개 LV2 전부**를 가져야 한다(빠지면 경고 + `tests/test_targeted.py` 실패).
`include`/`exclude`는 검색어에 들어가지 않고 rerank 가/감점 신호로만 쓴다 — Tavily에 부정 연산자가 없어
제외어를 쿼리에 넣으면 오히려 그 문서를 부른다.

### 수집 후 점검 — [phase2/review.py](src/phase2/review.py)

수집이 끝난 뒤에 하는 일은 한 파일에 모여 있다(수집 *중*의 저장 게이트인 `acceptance.py`와 구분).

| 하는 일 | 진입점 |
|---|---|
| 실행 리포트 조립 (coverage before→after · provider 성과 · 검색어별 · 계획별 누적) | 수집 끝에 자동 |
| **수동 표본 검수** — 본문 LLM 검증을 없앤 대가라 생략 불가 | `--sample-review <LV2>` |
| 저장 콘텐츠 OpenAI 재검수 (검색·fetch 재호출 없음) | Streamlit ② 2차 탭 |

```bash
python -m src.main --sample-review 6_O_CBRNE --sample-n 20 --db data/db/content.db
```

합격선: `domestic_direct precision ≥95%`, `LV2 precision ≥90%`, `combined ≥85%`.
표본 검수 없이 (a) 경로를 나머지 LV2로 확장하지 않는다.

### 튜닝

`streamlit run streamlit_app.py` → **② 2차 수집** 탭.
보강할 카테고리를 체크하고 **수집 기간**을 조절한 뒤 실행한다.
기간(`recency_days`) 하나로 Tavily `start_date`·SerpAPI `tbs`·acceptance의 `stale` 판정이 함께 움직인다.

**수집 목표는 부족분 순위를 매길 때만 쓰고 수집을 멈추지 않는다.** 목표를 이미 채운 카테고리도
고르면 그대로 검색한다. 이번 실행의 양은 UI **"이번 실행 수집량"** 두 슬라이더가 정한다.

| 손잡이 | 무엇을 묶나 | 비용 |
|---|---|---|
| 💳 카테고리당 검색어 수 (`query_planner.max_queries_per_lv2`) | 살 검색어 = 검색 API 호출 수 | **크레딧이 여기서만 나간다** (검색어 1개 = 1크레딧) |
| 본문 수집 상한 (`limits.max_total_fetch`) | 본문을 가져올 최대 건수 | 무료(HTTP) · 시간만 |

실행 전에 `예상 검색 호출 최대 N회`(검색어 수 × 선택 카테고리 수)를 화면에 표시한다.
실측: 검색어 3개 → 3호출·3크레딧, 8개 → 8호출·8크레딧으로 선형이다.
type별 `query_budget`을 먼저 채우고 넘치면 **앞에서부터 자른다(무작위 아님)**.
채널 배분은 `planner.source_mix`(예: `{web_news: 70, news_sites: 30}`)가 정하고, 없으면 균등하게 나눈다.
비율대로 **번갈아** 배치하므로 예산에 잘려도 두 채널이 모두 살아남는다.
검색 계획은 매번 새로 만들지 않고 seed 변경·7일 경과·성과 저조일 때만 재생성한다.

> 예전에는 `lv2_store_target`(목표)이 "이미 목표만큼 모았으면 검색어를 0개 산다"로 작동해서,
> 목표를 채운 카테고리는 아무 일도 없이 "완료"만 뜨고 저장 0건으로 끝났다(v23에서 제거).

설정은 [configs/targeted_collection.yaml](configs/targeted_collection.yaml).

> 개별 provider를 손으로 돌리던 0~3단계 UI는 제거했다(v23). taxonomy 수집 화면이 대체하며,
> 코드는 git history에 있다.

리포트에는 `provider_performance`(target_match_rate·korea_relevance_pass_rate·cost_per_stored),
`by_query`(검색어별 candidate→accepted), `query_plans`(계획별 발견→저장 누적), `coverage`(before→after)가 들어간다.

## 저장 정책

| 단계 | 결과 | 저장 |
| --- | --- | --- |
| rerank skip / URL·quality 탈락 | fail | `url_candidates` + `filter_logs` |
| 추출 실패 | fail | `url_candidates` + `filter_logs` |
| 판정 통과 | accepted | `content_records` (`action=accepted`) |
| near-dup | duplicate | `url_candidates` (`duplicate_of`) |

**판정 규칙은 모드마다 다르고, 코드도 각 phase 안에 따로 있다** — 통합하지 않는다.

| 모드 | 판정 | 파일 |
|---|---|---|
| 1차 trend | LLM 점수 → 로컬 임계값 | [phase1/verdict.py](src/phase1/verdict.py) `classification_action` |
| 2차 전략 LV2 | 결정론적 저장 게이트 | [phase2/acceptance.py](src/phase2/acceptance.py) `evaluate` |
| 2차 그 외 | 목표 taxonomy 신뢰 | [phase2/run.py](src/phase2/run.py) |
| 2차 사후 재검수 | 본문 LLM 재분류 | [phase2/review.py](src/phase2/review.py) `adjudicate` |

`url_candidates.status`는 모드 중립(`accepted`/`discard`/`candidate`/…)이고
1차·2차 구분은 `collection_phase` 컬럼이 한다.

`published_at`은 누락돼도 자동 탈락시키지 않고 리포트에 `missing_published_at_ratio`로 기록한다.

## 테스트

```bash
pytest                                  # 전체
pytest tests/test_common.py             # 공용 계층(저장·중복·마이그레이션·정제·리포트)
pytest tests/test_trend.py              # 1차 수집
pytest tests/test_targeted.py           # 2차 수집(계획 경로 + 고정 intent 경로)
pytest tests/test_acceptance.py tests/test_query_planner.py
```

`tests/conftest.py`가 LLM 호출을 기본 오프라인으로 강제한다(과금·비결정성 차단).
네트워크는 `fetcher.fetch` monkeypatch로 차단하고, discovery는 `parse_*(html, …)` 순수 함수로 검증한다.

## 알려진 제약

- GitHub은 수집원에서 제외했다(가져올 국내 콘텐츠가 거의 없음). discovery method와
  `serpapi.rules_by_lv2`의 `github.com` 도메인을 모두 제거했다.
- **본문 수집 불가로 실측 확인돼 검색 대상에서 뺀 도메인**(2026-08). JS 렌더·유료 rung을 두지 않으므로
  "정적으로 안 되면 못 쓴다"가 실질 기준이다. `tests/test_targeted.py`가 재유입을 막는다.

  | 도메인 | 사유 |
  |---|---|
  | fmkorea.com · pann.nate.com | robots `User-agent: *` → `Disallow: /` |
  | velog.io | SPA — 정적 HTML에 본문 0자 |
  | blog.naver.com | iframe 구조 — 정적 HTML에 본문 0자 |
  | me.go.kr | 기후에너지환경부(`mcee.go.kr`)로 개편, 리디렉트 셸만 남음 |
- `source_strategies_by_lv2`는 19개 LV2 전부 작성돼 있다. 다만 (a) 경로는 본문 LLM 검증을
  하지 않으므로 **표본 검수 없이 신뢰하지 않는다** (위 §수집 후 점검).
