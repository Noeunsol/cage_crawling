# cage_crawling — Taxonomy-aware Korean Web Content Crawler

Risk Taxonomy(19 LV2 / 67 type)에 맞는 한국 웹 콘텐츠를 수집하는 **Focused Crawler**.
SerpAPI·Tavily·OpenAI는 모두 실제 API를 호출하며, 키가 없으면 결정론적 Mock/오프라인으로 떨어진다.

폴더 책임과 데이터 흐름은 [ARCHITECTURE.md](ARCHITECTURE.md) 참고.

## 실행

```bash
pip install -r requirements.txt && cp .env.example .env   # 키 채우기

python -m src.main --mode trend -v         # 1차: 디시·뉴스 트렌드 수집
python -m src.main --mode targeted -v      # 2차: 부족 taxonomy 보강
python -m src.main --mode keyword -v       # 레거시 키워드 검색 (SerpAPI)

python -m src.main --mode targeted --dry-run   # 부족 LV2 랭킹만 프리뷰(비용 없음)
python -m src.main --reset-db                  # DB 삭제 후 재생성
streamlit run streamlit_app.py                 # 관찰 + 2차 실행 UI
```

기본 산출물: `data/db/content.db`(sqlite), `data/exports/report.json`(stdout에도 출력).

키: `SERPAPI_KEY`(커뮤니티 `site:` 검색), `TAVILY_API_KEY`(의미 검색), `OPENAI_API_KEY`(분류·검색계획).
없으면 해당 경로만 Mock/스킵된다.

## 세 가지 모드

| 모드 | 무엇을 하나 | 최종 분류 |
| --- | --- | --- |
| `trend` | 디시 갤러리·일베·닥터나우·뉴스 RSS 최신글을 기간 기준으로 훑는다 | OpenAI가 본문을 보고 taxonomy 결정 |
| `targeted` | 부족한 LV2를 목표로 정밀 보강 (아래 §2차 수집) | 전략 LV2는 규칙 게이트, 나머지는 목표 LV2 신뢰 |
| `keyword` | taxonomy 키워드 → SerpAPI 검색 (레거시) | rule → LLM 2단계 matcher |

`trend`는 건수 목표를 채우지 않고 기간 내 가용 후보를 제목 필터 → 본문 수집 → LLM taxonomy 순으로 처리한다.

## 파이프라인

```
discovery(URL 후보) → rerank(fetch 전 선별) → fetch → extract(본문)
  → clean → quality gate → 분류/판정 → near-dup → sqlite → report
```

- **Search는 URL 발견, Extractor는 본문 추출**(역할 분리). provider의 title/snippet은
  후보 rerank 메타일 뿐이며 **`ContentRecord` 본문으로 저장하지 않는다**.
- 모든 발견 URL은 최종 상태와 함께 `url_candidates`에 남는다. 이전 실행의 `content_records`까지
  canonical URL / dedup hash / event key / SimHash로 중복 검사한다.

### 추출 사다리 (Cost-Escalation Ladder)

싼 로컬 추출부터 시도해 site_type별 성공 기준을 만족하는 첫 rung에서 멈춘다 ([extract/router.py](src/extract/router.py)).

| site_type | rung 순서 |
| --- | --- |
| news/blog/tech | trafilatura → playwright* → firecrawl* |
| qna | naver_kin(`__NEXT_DATA__`) → trafilatura → playwright* → firecrawl* |
| community/dynamic | dcinside/community(bs4 본문+댓글) → playwright* → firecrawl* |

`*` = gated seam(기본 off, `extraction.{playwright,firecrawl}.enabled`). 정적 fetch는
[fetcher.py](src/fetcher.py)(UA/timeout/per-domain delay/robots 준수)로 1회, 정적 rung들이 공유한다.

### 본문 3단 + 분류

- `raw_text`(원문, export 기본 제외) / `cleaned_text`(boilerplate 제거) / `core_text`(화면·품질·LLM 기본 본문).
  욕설·협박은 **보존**한다(Toxic Language의 raw 가치).
- ⚠️ **PII 마스킹은 현재 수행되지 않는다.** `masked_text`는 `cleaned_text`의 별칭이며
  ([clean.py](src/clean.py)), `src/mask.py`는 어느 파이프라인에도 연결돼 있지 않다.
  `pii_*` / `masking_*` / `masked_entities` 컬럼은 항상 비어 있다.
- 분류 LLM은 **OpenAI `gpt-4o-mini`**([configs/llm.yaml](configs/llm.yaml)). 프롬프트에 injection 방어 문구가
  들어가고 JSON 파싱 실패 시 rule fallback 또는 fail-closed discard.

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
  저장 목표량(`lv2_store_target`) 기준으로 쓴다.
- 저장 조건은 `domestic_direct AND 목표 LV2 evidence AND 최근성 AND 본문 품질 AND 비중복`
  ([phase2/acceptance.py](src/phase2/acceptance.py)). 통과하면
  `classification_source = targeted_acceptance_gate`로 기록된다 — 예측값이 아니라 **게이트 통과로 확정된 값**이다.
- source의 `access`가 `direct`가 아니면 본문을 가져오지 않고, `blocked`면 API 호출조차 하지 않는다.
- `modes`에 `official_seed`가 있으면 2-hop: 공식기관 원문을 최종 콘텐츠로 저장하고,
  그 원문을 seed로 후속 보도를 한 번 더 검색한다.
- 검색 계획은 매번 새로 만들지 않는다. **seed 변경 / 7일 경과 / 성과 저조**일 때만 재생성하고
  그 외에는 `query_plans` 테이블의 계획을 재사용한다(OpenAI 호출 0회).

`recency_days`는 LV2별로 다르다(커뮤니티 표현 90일, CBRNE 730일 등).

### (b) 고정 intent 경로 — 나머지 16개 LV2

`collection_intents_by_lv2`의 손으로 쓴 검색어를 Tavily/SerpAPI에 그대로 보내고,
`adjudication.openai_verification`이 꺼져 있으면 목표 taxonomy를 신뢰해 저장한다.
`collection_intents_by_lv2`는 **19개 LV2 전부**를 가져야 한다(빠지면 경고 + `tests/test_targeted.py` 실패).
`include`/`exclude`는 검색어에 들어가지 않고 rerank 가/감점 신호로만 쓴다 — Tavily에 부정 연산자가 없어
제외어를 쿼리에 넣으면 오히려 그 문서를 부른다.

### 수동 표본 검수 (필수)

(a) 경로는 본문 LLM 검증을 하지 않으므로 표본 검수 없이 나머지 LV2로 확장하지 않는다.

```bash
python -m src.main --sample-review 6_O_CBRNE --sample-n 20 --db data/db/content.db
```

합격선: `domestic_direct precision ≥95%`, `LV2 precision ≥90%`, `combined ≥85%`.

### 튜닝

Streamlit "🎯 2차 타깃 수집" 탭에서 ① 검색문 미리보기 → ② 검색 미리보기 → ③ 수집·저장을
반복한다. **①②는 고정 intent 경로 전용**이다(전략 LV2는 실행 시점에 계획을 만든다).
설정은 [configs/targeted_collection.yaml](configs/targeted_collection.yaml).

리포트에는 `provider_performance`(target_match_rate·korea_relevance_pass_rate·cost_per_stored),
`by_query`(검색어별 candidate→accepted), `query_plans`(계획별 발견→저장 누적), `coverage`(before→after)가 들어간다.

## 저장 정책

| 단계 | 결과 | 저장 |
| --- | --- | --- |
| rerank skip / URL·quality 탈락 | fail | `url_candidates` + `filter_logs` |
| 추출 실패 | fail | `url_candidates` + `filter_logs` |
| 판정 통과 | accepted | `content_records` (`action=accepted`) |
| near-dup | duplicate | `url_candidates` (`duplicate_of`) |

판정 임계는 모드마다 다르다 — [pipelines/taxonomy_adjudication.py](src/pipelines/taxonomy_adjudication.py)의
세 함수(`_classification_status`/`_trend_classification_action`/`_phase2_adjudicate`)가 각각의 정책 계약이고,
전략 LV2는 이 셋 대신 [phase2/acceptance.py](src/phase2/acceptance.py)를 쓴다.

`published_at`은 누락돼도 자동 탈락시키지 않고 리포트에 `missing_published_at_ratio`로 기록한다.

## 테스트

```bash
pytest                                  # 전체
pytest tests/test_targeted.py           # 2차 수집(계획 경로 + 고정 intent 경로)
pytest tests/test_acceptance.py tests/test_query_planner.py
```

`tests/conftest.py`가 LLM 호출을 기본 오프라인으로 강제한다(과금·비결정성 차단).
네트워크는 `fetcher.fetch` monkeypatch로 차단하고, discovery는 `parse_*(html, …)` 순수 함수로 검증한다.

## 알려진 미연결 항목

- `src/mask.py` — PII 마스킹 구현은 있으나 파이프라인에 연결돼 있지 않다(위 참고).
- `serpapi.rules_by_lv2`의 `mode` / `query_strategy` / `append_terms` / `append_terms_by_type` /
  `reference_domains_by_type` / `deprioritized_domains_by_type` — 설정에는 있으나 코드가 읽지 않는다.
- `exa` / `github` discovery — 라우터 seam만 있고 클라이언트 미구현.
- `source_strategies_by_lv2` — 19개 중 3개만 작성됨(위 표본 검수 통과 후 확장).
