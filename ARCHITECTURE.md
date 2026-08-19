# 아키텍처 개요 — cage_crawling

한국 웹(디시·뉴스·커뮤니티) 콘텐츠를 안전 taxonomy로 수집·분류하는 크롤러.
"처음 보는 동료"가 흐름을 따라갈 수 있게 폴더 책임과 단계별 데이터 흐름을 요약한다.

## 폴더 책임

| 폴더 | 책임 |
|---|---|
| `prompts/` | 모든 LLM 시스템 프롬프트(yaml). rubric·판정규칙·예시·형식만. 카테고리 목록은 렌더 시 주입. |
| `configs/` | 설정 + 메타데이터. `taxonomy.yaml`(라벨 스키마·단일 정본), `site_policy.yaml`(도메인 라우팅), `crawler_settings.yaml`(임계값·staging), `llm.yaml`(모델/가격), `*_collection.yaml`(모드별 수집). `archive/`=옛 설정. |
| `data/` | `db/`(SQLite 실질 저장소), `exports/`(report·csv), `stages/<content_id>/`(단계별 파일·opt-in), `archive/`(잡동사니). |
| `logs/` | `app.log`(회전) + `llm_usage.jsonl`(호출별 토큰·비용 감사). |
| `scripts/` | 모드별 얇은 실행 래퍼(`run_trend`/`run_targeted`/`run_keyword`) → `src/main.py`로 위임. |
| `src/` | 핵심 로직(아래). |
| `tests/` | 단위·통합 테스트. `conftest.py`가 LLM을 기본 오프라인으로 강제(과금·비결정성 차단). |

## src/ 구조

- **진입점**: `main.py`(CLI `--mode {keyword,trend,targeted}`), `pipeline.py`(facade — 실제 구현은 `pipelines/`로 재export).
- **실행 모드** `pipelines/`: `keyword.py`(레거시 검색), `trend.py`(디시·뉴스 트렌드), `gap_filling.py`(2차 semantic 보강).
  공용 헬퍼: `stages.py`(matcher 조립·usage 복사), `taxonomy_adjudication.py`(3개 판정기), `persist.py`(저장 마무리), `_trend_util.py`(버킷·윈도우·링크).
- **foundation leaf**(거의 모든 모듈이 import): `schema.py`(데이터 계약 + `content_id_for`), `policy.py`(taxonomy 로더), `site_registry.py`, `prompt_loader.py`, `fetcher.py`.
- **수집(discovery)**: `discovery/`(board·rss·router 등). 2차는 `phase2/`:
  `provider.py`(Tavily·SerpAPI) · `intent_builder.py`(고정 intent) · `reranker.py`(fetch 전 선별) ·
  `query_planner.py`(OpenAI 검색 계획) · `source_router.py`(method 분기) · `acceptance.py`(결정론적 저장 게이트).
- **추출(extract)**: `extract/`(router 사다리 + site_parser + gated seam).
- **분류/정제/저장**: `classify/matcher.py`(rule→LLM), `clean.py`, `filtering/`(quality·relevance·risk_signals·url_filter·korea_context),
  `storage/`(store·dedup), `reporting/`(report·coverage).
- **공용 유틸**: `paths.py`(경로 중앙화), `logging_setup.py`, `llm_tracker.py`, `artifact_store.py`.
- **미연결**: `mask.py`(PII 마스킹 구현은 있으나 어느 파이프라인도 호출하지 않는다).

## 단계별 데이터 흐름 (수집 → 정제 → 분류 → 저장)

```
discovery(URL 후보) → rerank(fetch 전 선별) → fetch(HTML) → extract(본문)
   → clean(cleaned_text = core_text = masked_text) → quality gate
   → 판정: LLM classify + adjudicate  |  또는  결정론적 acceptance gate(전략 LV2)
   → dedup(near-dup) → store → report/export
```

- 각 단계는 **DB 컬럼**으로 표현된다(`content_records`: raw_text→cleaned_text→core_text→taxonomy_lv2/action). SQLite가 실질 단계 저장소.
- ⚠️ **PII 마스킹 단계는 현재 없다.** `clean_record`가 `masked_text`를 `cleaned_text` 별칭으로 채울 뿐이며,
  `pii_*`/`masking_*`/`masked_entities` 컬럼은 항상 비어 있다.
- `staging.enabled=true`면 각 단계 텍스트가 추가로 `data/stages/<content_id>/`에 파일로 남는다(재현·디버깅용, 기본 off). `raw_text`/`cleaned`는 `privacy.save_raw_text`로 재게이트 + gitignore.
- LLM 분류는 taxonomy 개념을 `configs/taxonomy.yaml`에서, 판정 rubric을 `prompts/taxonomy_mapping.yaml`에서 렌더. 모델은 leaf `category`(type)만 고르고 lv1/lv2는 코드가 역산.
- 2차 전략 LV2는 본문 LLM 분류 대신 `phase2/acceptance.py`가 저장을 판정하고,
  OpenAI는 `prompts/query_plan.yaml`로 **검색 계획 생성에만** 쓰인다.

## 실행

```bash
pip install -r requirements.txt && cp .env.example .env   # 키 채우기
python -m src.main --mode trend -v            # 또는 scripts/run_trend.py
python -m src.main --mode targeted --dry-run  # 2차 부족분 프리뷰
streamlit run streamlit_app.py                # 관찰·2차 실행 UI
```
