# 아키텍처 개요 — cage_crawling

한국 웹(디시·뉴스·커뮤니티) 콘텐츠를 안전 taxonomy로 수집·분류하는 크롤러.
"처음 보는 동료"가 흐름을 따라갈 수 있게 폴더 책임과 단계별 데이터 흐름을 요약한다.

## 폴더 책임

| 폴더 | 책임 |
|---|---|
| `configs/` | 설정 기본값. `taxonomy.yaml`(라벨 스키마·단일 정본), `site_policy.yaml`(도메인 라우팅), `crawler_settings.yaml`(1·2차 공용 임계값), `llm.yaml`(모델/가격), `trend_collection.yaml`(1차), `targeted_collection.yaml`(2차). |
| `data/` | 콘텐츠 저장. `db/`(SQLite 실질 저장소), `exports/`(report·csv), `archive/`(옛 실행 잡동사니). |
| `prompts/` | LLM 시스템 프롬프트(yaml)만. 판정 규칙·형식·예시. 카테고리와 rubric은 렌더 시 주입. |
| `src/` | 메인 코드. `common/`(공용) + `phase1/`(1차) + `phase2/`(2차). |
| `logs/` | `app.log`(회전) + `llm_usage.jsonl`(호출별 토큰·비용 감사). |
| `scripts/` | 모드별 얇은 실행 래퍼(`run_trend`/`run_targeted`) → `src/main.py`로 위임. |
| `tests/` | 단위·통합 테스트. `conftest.py`가 LLM·검색 API를 기본 오프라인으로 강제(과금·비결정성 차단). |

## src/ 구조 — 1차와 2차는 구현이 다르다

**핵심 규칙: `phase1`과 `phase2`는 서로 import하지 않는다.** 공유가 필요하면 `common/`으로 내린다.
두 수집은 목표도 판정 방식도 다르기 때문에 코드를 합치지 않고 폴더로 갈라 둔다.

```
src/
  main.py                  CLI — 인자만 받아 phase1/phase2로 위임, 공통 로직 없음

  common/                  1차·2차 공용 foundation (phase1/phase2를 import하지 않는다)
    schema.py              데이터 계약 + content_id_for/canonicalize_url
    policy.py              taxonomy.yaml 로더 (Policy/Subtype + llm_rubric)
    paths.py               모든 기본 경로·설정 파일 위치 (하드코딩 금지)
    site_registry.py       도메인 → site_name/site_type
    prompt_loader.py       prompts/*.yaml 로더 + rubric 렌더
    fetcher.py             정적 HTTP (UA/timeout/per-domain delay/robots/연속실패 차단)
    clean.py               boilerplate 제거 → cleaned_text/core_text/body_text
    classify.py            LLMMatcher(본문 → taxonomy) + 분류기 조립 + 위험/트렌드 스코어
    report.py              build_report / export_report / export_csv / print_report
    extract/               추출 사다리 (router + trafilatura + site_parser)
    storage/               store(SQLite) + dedup(simhash·event key)
    filtering/             quality · relevance_filter · korea_context · risk_signals
    sources/               board(디시·일베·닥터나우) · rss(뉴스). 2차도 board_list를 재사용

  phase1/                  1차 수집 — 기간 기준으로 훑는다
    run.py                 트렌드 수집 파이프라인
    sampling.py            주제·시간 버킷 표본 추출
    verdict.py             LLM 점수 → accepted/discard 판정 + 저장 마무리

  phase2/                  2차 수집 — 부족 taxonomy를 목표로 보강한다
    run.py                 수집 엔진 (small_run / run_targeted / run_taxonomy_plan)
    config.py              targeted_collection.yaml 로더 + override 병합
    coverage.py            LV2별 부족분 계산
    intent_builder.py      고정 intent 경로의 검색어 조립
    query_planner.py       OpenAI 검색 계획 생성 + 코드 측 검증 규칙
    provider.py            Tavily · SerpAPI 클라이언트
    source_router.py       source의 method 분기 (serpapi / web_search / board_list)
    reranker.py            fetch 전 후보 선별
    acceptance.py          결정론적 저장 게이트 (수집 "중" 판정)
    review.py              수집 "후" 점검 — 리포트·표본검수·재검수 (아래 참고)
```

## 단계별 데이터 흐름

```
discovery(URL 후보) → rerank(fetch 전 선별) → fetch(HTML) → extract(본문)
   → clean(cleaned_text = core_text = body_text) → quality gate
   → 판정: 1차=LLM 본문 분류  |  2차=결정론적 acceptance gate 또는 목표 LV2 신뢰
   → dedup(near-dup) → store → report
```

- 각 단계는 **DB 컬럼**으로 표현된다(`content_records`: raw_text→cleaned_text→core_text→taxonomy_lv2/action).
  SQLite가 실질 단계 저장소다.
- `url_candidates.status`는 **모드 중립**이다(`accepted`/`discard`/`candidate`/`duplicate`/…).
  1차·2차 구분은 `collection_phase`(1/2) 컬럼이 한다.
- **PII 마스킹 단계는 없다.** 욕설·협박 보존이 Toxic Language 수집의 목적이다.
  LLM에는 정제 본문(`core_text`)만 보내고 `raw_text`는 어떤 경로로도 보내지 않는다.
- **JS 렌더·유료 추출 rung은 없다.** 정적 fetch 1회를 모든 rung이 공유하며,
  "정적으로 안 되면 수집하지 않는다"가 실질 기준이다.

## 분류 — taxonomy와 rubric이 프롬프트로 간다

`configs/taxonomy.yaml`의 각 type에 붙은 `llm_rubric`(전제/포함/제외/경계)이
`prompts/taxonomy_mapping.yaml`의 `type_format`을 통해 **실제 시스템 프롬프트로 들어간다**
(`prompt_loader.render_rubric`). `few_shot_examples`는 길이 때문에 제외하고,
판정 예시는 프롬프트 파일의 `examples` 선별본이 담당한다.

taxonomy 블록은 약 43k자다. 호출마다 동일하므로 OpenAI 자동 prompt caching이 걸린다
(실측: 입력 27k 토큰 중 약 25.7k가 캐시 히트).

## 수집 목표는 상한이 아니다

`configs/targeted_collection.yaml`의 `target_selection.min_accepted_per_lv2`(모든 LV2 공통 100)가
수집 목표의 **단일 정본**이다. 이 값은 "어느 LV2가 얼마나 모자란가"를 재는 데만 쓰고
수집을 멈추지 않는다 — 목표를 채운 LV2도 고르면 그대로 검색한다.
실행량을 묶는 손잡이는 둘뿐이고, Streamlit "이번 실행 수집량"에서 실행마다 조절한다.
  `query_planner.max_queries_per_lv2` — 살 검색어 수. **검색 크레딧이 여기서만 나간다.**
  `limits.max_total_fetch` — 본문 fetch 상한. HTTP만 쓰므로 무료고 시간만 든다.
`run_taxonomy_plan(max_queries_per_lv2=..., max_total_fetch=...)`가 config를 건드리지 않고
override로 얹는다.

## 2차 수집 후 점검 — `phase2/review.py`

수집이 끝난 뒤에 하는 일은 한 파일에 모여 있다. 수집 *중*의 저장 게이트(`acceptance.py`)와는
성격이 다르다 — 그건 건건이 저장 여부를 정하고, 이건 이미 저장된 것을 되돌아본다.

| 함수 | 하는 일 | 진입점 |
|---|---|---|
| `build_run_report` | coverage(before→after) · provider 성과 · 검색어별 · 계획별 누적 | `run.py`가 수집 끝에 1회 호출 |
| `sample_for_review` | 수동 표본 검수 CSV. 본문 LLM 검증을 없앤 대가라 생략 불가 | `--sample-review <LV2>` |
| `verify_unverified_candidates` | 저장된 본문만 OpenAI로 재분류(검색·fetch 재호출 없음) | Streamlit ② 2차 탭 |
| `adjudicate` | 재검수가 쓰는 본문 기준 판정 규칙 | 위 함수가 사용 |

**수집 중에는 본문을 LLM으로 재분류하지 않는다.** 저장 판정은 규칙 게이트가 하고,
LLM 본문 검수는 위 재검수 경로로만 돈다.

## 실행

```bash
pip install -r requirements.txt && cp .env.example .env   # 키 채우기
python -m src.main --mode trend -v             # 1차
python -m src.main --mode targeted --dry-run   # 2차 부족분 프리뷰(비용 0)
streamlit run streamlit_app.py                 # 관찰 + 실행 UI (통합 / ① 1차 / ② 2차)
```
