# cage_crawling — Taxonomy-aware Korean Web Content Crawler

Risk Taxonomy에 맞는 한국 웹 콘텐츠를 자동 수집하는 **Focused Crawler**.
현재는 **1차 mock end-to-end 스켈레톤** — 실제 외부 API 호출 없이(SerpAPI/Tavily/
Firecrawl 등) 결정론적 mock으로 Phase 0→12 전체 파이프라인이 한 번에 실행된다.

## 실행

```bash
pip install -r requirements.txt
python -m src.main --config configs/taxonomy_policy.yaml -v
```

실행 결과:
- `data/content.db` — sqlite (content_records / url_candidates / filter_logs)
- `data/exports/report.json` — coverage/소스분포/필터로그/품질 리포트 (stdout에도 출력)

## 파이프라인 (설계서 §3)

```
taxonomy_policy(Phase0) → QueryGenerator(1) → SearchRouter(2) → UrlFrontier(3)
→ URL filter(4) → ExtractorRouter(5-7) → clean+PII(8) → QualityFilter(9)
→ TaxonomyMatcher(10) → sqlite(11) → Report(12)
```

핵심 원칙: **Search는 URL 발견, Extractor는 본문 추출** (역할 분리). 모든 추출 결과는
동일한 `ContentRecord` 스키마로 표준화되고, 저장 전 PII 마스킹 + 품질/taxonomy 검증을 거친다.

## 수집 전략 (하이브리드 focused crawling)

키워드-only는 은어·우회표현·맥락 사례를 놓치므로 여러 경로를 병렬로 둔다. 각 후보에
`collection_method`를 태그해 리포트에서 경로별 성능을 비교한다 ([collection.py](src/collection.py)).

| 전략 | 설명 | 상태 |
| --- | --- | --- |
| `keyword` | 키워드 OR 검색 (SerpAPI) | ✅ mock |
| `semantic` | subtype 설명/자연어로 의미 검색 (Tavily) | ✅ mock |
| `site_sampling` | 인기글을 키워드 없이 샘플링 → matcher가 분류 | ✅ mock |
| `seed_expansion` | 고신뢰 문서 주변 링크 확장 | ⏳ seam (추출 후 링크 필요) |
| `trend` | 인기글에서 신조어 추출 → 사람 검토 | ⏳ seam (LLM/통계 필요) |

`semantic`/`site_sampling` 경로는 URL 필터의 키워드 게이트를 건너뛰고 **matcher가 최종
분류**한다(샘플링 노이즈는 여기서 걸러짐). 활성 전략은 `configs/crawler_settings.yaml`의
`collection.enabled`로 조정.

## 채택 도구 (1차)

- 검색: **SerpAPI + Tavily** (mock). Exa는 라우터 seam만.
- 추출: **Firecrawl(범용) + NaverKin(site parser)** (mock). Crawl4AI/browser-use는 라우터 분기 규칙만.

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
Crawl4AI/browser-use + 커뮤니티 파서 → LLM TaxonomyMatcher → 19개 taxonomy 정책.
