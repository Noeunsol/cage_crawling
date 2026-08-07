# cage_crawling — Taxonomy-aware Korean Web Content Crawler

Risk Taxonomy에 맞는 한국 웹 콘텐츠를 자동 수집하는 **Focused Crawler**.
현재는 **1차 end-to-end 스켈레톤** — SerpAPI 검색은 실제 SDK를 사용하고,
Tavily/Firecrawl 등 나머지 외부 연동은 아직 결정론적 mock이다.

## 실행

```bash
pip install -r requirements.txt
# 1차 대량 수집은 OCR을 사용하지 않으므로 Tesseract 설치가 필요 없다.
export SERPAPI_KEY=...                       # keyword 검색(real). 없으면 검색 단계 실패
python -m src.main -v                         # 실제 수집 (SerpAPI + fetch)
python -m src.main --dry-run                  # fetch 없이 수집 예정 범위·도메인 분포 프리뷰
python -m src.main --reset-db                 # 기존 DB 삭제 후 재생성(스키마 마이그레이션)
```

관찰용 Streamlit 대시보드:

```bash
streamlit run streamlit_app.py
```

Trend 수집은 기본적으로 최근 1일(24시간)을 대상으로 하며 Streamlit의 `최근 며칠`에서 조절한다.
건수 목표를 채우지 않고 기간 내 가용 후보를 제목 필터 → 본문 수집 → LLM taxonomy 순으로 처리한다.

대시보드는 `data/content.db`를 read-only로 열며 masked 본문, 후보 상태, 실패 사유,
PII 메타데이터와 단계별 전환율만 표시한다. raw 본문과 raw 댓글은 표시하지 않는다.

선택 활성화: `matching.llm.enabled: true` + `ANTHROPIC_API_KEY`(Claude Haiku 애매구간 분류),
`extraction.{playwright,firecrawl}.enabled: true`(+ 해당 lib 설치)로 gated rung 켜짐.
오프라인 테스트는 `fetcher.fetch`를 fixture로 monkeypatch → `pytest tests/test_pipeline.py`.

실행 결과:
- `data/content.db` — sqlite (content_records / url_candidates / filter_logs)
- `data/exports/report.json` — coverage/소스분포/필터로그/품질 리포트 (stdout에도 출력)

## 파이프라인 (설계서 §3)

```
taxonomy.yaml(Phase0) → QueryGenerator(1) → SearchRouter(2) → UrlFrontier(3)
→ URL filter(4) → ExtractorRouter(5-7) → clean+PII(8) → QualityFilter(9)
→ TaxonomyMatcher(10) → sqlite(11) → Report(12)
```

현재 실행 앞단은 `TaxonomyPolicy → StrategyRouter → StrategyTask → DiscoveryRouter`로
구성된다. taxonomy는 최종 라벨, strategy는 수집 경로, safety overlay는 저장 전
마스킹/보존 정책이다. `board_list`·Exa·GitHub는 명시적 adapter seam이며 Tavily는
결정론적 mock이다. RSS/Sitemap/Seed URL과 SerpAPI는 실제 discovery 경로를 제공한다.

모든 발견 URL은 최종 상태와 함께 `url_candidates`에 남고, 이전 실행의
`content_records`까지 canonical URL/dedup hash/event key/SimHash로 중복 검사한다.
`report.json`에는 strategy 및 API별 discovered→extracted→pass/review 전환율이 포함된다.

핵심 원칙: **Search는 URL 발견, Extractor는 본문 추출** (역할 분리). 모든 추출 결과는
동일한 `ContentRecord` 스키마로 표준화되고, 저장 전 PII 마스킹 + 품질/taxonomy 검증을 거친다.

## 수집 전략 (하이브리드 focused crawling)

키워드-only는 은어·우회표현·맥락 사례를 놓치므로 여러 경로를 병렬로 둔다. 각 후보에
`collection_method`를 태그해 리포트에서 경로별 성능을 비교한다 ([collection.py](src/collection.py)).

| 전략 | 설명 | 상태 |
| --- | --- | --- |
| `keyword` | 키워드 OR 검색 (SerpAPI) | ✅ real API |
| `semantic` | subtype 설명/자연어로 의미 검색 (Tavily) | ✅ mock |
| `site_sampling` | 인기글을 키워드 없이 샘플링 → matcher가 분류 | ✅ mock |
| `seed_expansion` | 고신뢰 문서 주변 링크 확장 | ⏳ seam (추출 후 링크 필요) |
| `trend` | 인기글에서 신조어 추출 → 사람 검토 | ⏳ seam (LLM/통계 필요) |

`semantic`/`site_sampling` 경로는 URL 필터의 키워드 게이트를 건너뛰고 **matcher가 최종
분류**한다(샘플링 노이즈는 여기서 걸러짐). 활성 전략은 `configs/crawler_settings.yaml`의
`collection.enabled`로 조정.

## 추출 사다리 (Cost-Escalation Ladder)

싼 로컬 추출부터 시도해 site_type별 성공 기준을 만족하는 첫 rung에서 멈춘다 ([extract.py](src/extract.py)).
**fetch는 requests(정적)/Playwright(JS 렌더), 추출은 trafilatura로 통일**(일관된 단일 추출 엔진).
Playwright/Firecrawl은 config로 gated(기본 off), lib은 lazy import. Firecrawl은 value_score ≥ min_value일 때만.

| site_type | rung 순서 |
| --- | --- |
| news/blog/tech | trafilatura → playwright* → firecrawl* |
| qna | naver_kin(`__NEXT_DATA__`) → trafilatura → playwright* → firecrawl* |
| community/dynamic | community(bs4 본문+댓글) → playwright*(렌더+trafilatura) → firecrawl* |

`*` = gated seam(기본 off). 성공 기준은 site_type별로 다름(뉴스 400자 / 커뮤니티 80자+댓글 등).
정적 fetch는 [fetcher.py](src/fetcher.py)(UA/timeout/per-domain delay/robots 준수)로 1회, 정적 rung들이 공유.

## raw 보존 + 2단계 분류

- **raw/cleaned/masked 3단 분리** ([clean.py](src/clean.py)): 욕설·협박은 보존, PII만 마스킹.
  `raw_text`(원문, export 제외) / `cleaned_text` / `masked_text`(=body_text, matcher·LLM 입력).
- **2단계 matcher** ([matcher.py](src/matcher.py)): rule 먼저 → 애매 band·media ambiguity·high-value 충돌에서만
  **Claude Haiku** 호출(`matching.llm.enabled`, 기본 off). LLM은 masked만 받고 injection 방어 문구 포함, JSON 파싱 실패 시 rule fallback.
- **동일 사건 near-dup** ([dedup.py](src/dedup.py)): SimHash + event_key. URL-dedup과 별도로 관리.
- **filter_mode**: Toxic Language는 `minimal`(키워드 게이트 완화) + strong post-matcher. hard-negative는 항상 적용.

## 검색 도구

- 검색: **SerpAPI** (real API) + **Tavily** (mock). Exa는 라우터 seam만.

## 2차 semantic 보강 수집 (`--mode targeted`)

1차 trend 수집이 남긴 부족 taxonomy를 **의미 기반으로 정밀 보강**한다. 키워드/`site:` 검색이 아니라
`taxonomy.yaml 기준 → 자연어 collection intent → Tavily 후보 발견 → LLM rerank(fetch 전) →
기존 fetch/extract/mask/LLM 재분류 → 한국 관련성 검증`으로 저장 여부를 결정한다. API snippet/
content_hint는 discovery 메타로만 쓰고 **본문으로 저장하지 않는다**.

```bash
export TAVILY_API_KEY=...   # 없으면 Mock provider(오프라인). 분류는 OPENAI_API_KEY 사용
python -m src.main --mode targeted --dry-run   # 부족 LV2 랭킹 + LV2별 자연어 intent 프리뷰
python -m src.main --mode targeted -v          # deficit까지 실제 보강 수집
```

튜닝은 **Streamlit 파일럿 러너**(페이지 하단 "🎯 2차 타깃 수집") 중심 — ① Intent Preview →
② Discovery Preview(Tavily 품질·rerank threshold 육안 튜닝) → ③ Small Run(scratch DB 기본, limit
소량)로 "조절 → 실행 → 확인 → 재조정"을 반복한다. 설정: `configs/phase2_semantic_collection.yaml`
(부족 판정 `min_accepted_per_lv2`/`targets_by_lv2`, LV2별 `collection_intents_by_lv2`, `sensitive_overlay`,
rerank·acceptance threshold, 실행 상한 `limits`). target(`taxonomy_lv2_candidate`) vs predicted
(`taxonomy_lv2`)는 분리 저장되고, 리포트에 `provider_performance`(target_match_rate·
korea_relevance_pass_rate·cost_per_accepted)와 `coverage`(before→after)가 포함된다.

## 저장 정책

| 단계 | 결과 | 저장 |
| --- | --- | --- |
| URL/quality 탈락 | fail | `filter_logs`만 |
| 추출 실패 | fail | `url_candidates` + `filter_logs` |
| confidence ≥ 0.8 | pass | `content_records` |
| 0.5 ≤ confidence < 0.8 | review | `content_records` (filter_status=review) |
| confidence < 0.5 | fail | `filter_logs`만 |

`published_at`은 누락돼도 자동 탈락시키지 않고 리포트에 `missing_published_at_ratio`로 기록.

## 테스트

```bash
pytest tests/test_pipeline.py
```

## 다음 단계 (설계서 §7)

실제 SerpAPI/Tavily 클라이언트 → site parser DOM/`__NEXT_DATA__` 파싱 →
Playwright 렌더 + 커뮤니티 댓글 selector 추출 → LLM TaxonomyMatcher → 19개 taxonomy 정책.
