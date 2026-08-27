### 1. 레포지토리 설명
연관 프로젝트: 텍소노미에 필요한 원천 문서를 자동으로 수집하는 기술

담당자: 노은솔

작성일: 2026-08-27

한국어 커뮤니티/뉴스/전문 매체 사이트를 대상으로, AI Safety Risk Taxonomy(LV1/LV2/type 3단 분류 체계)에
속하는 실제 사례 콘텐츠를 수집하는 파이프라인이다. SerpAPI(`site:` 검색)와 Tavily 두 검색 프로바이더로
후보 URL을 찾고(discovery), 사이트별 파서 또는 범용 추출기(trafilatura)로 본문을 뽑아낸 뒤(extraction),
블랙리스트/중복/기간/한국어 비율/LLM taxonomy 적합성 순으로 필터링해서(filtering) 최종 통과한
콘텐츠만 `data/final/[LV2]/[type].csv`로 내보낸다(storage). Streamlit UI(`app.py`)에서 수집 설정 →
도메인 설정 → 검색어 검토 → 실행 및 결과 → 데이터 탐색까지 5단계로 전체 과정을 조작한다.

### 2. 데이터셋
설명:
- 원본 출처: `configs/type_domains.yaml`에 taxonomy type별로 등록된 한국 뉴스·커뮤니티·전문 매체
  도메인(예: newsis.com, hani.co.kr, gall.dcinside.com, lawtalk.co.kr 등)에서 SerpAPI/Tavily 검색으로
  찾은 실제 게시글.
- 제작 방법: 검색 → 본문 추출(trafilatura 또는 사이트 전용 파서) → 블랙리스트/중복/기간/한국어 비율
  필터 → OpenAI 기반 taxonomy 적합성 판단(`prompts/taxonomy_filtering.yaml`)을 통과한 건만 채택.
- 스키마 (CSV 컬럼, `src/storage/csv_exporter.py:CSV_COLUMNS`):

  | 컬럼 | 설명 |
  |---|---|
  | title | 게시글 제목 |
  | content | 정제된 본문 텍스트 |
  | date | 게시일(YYYY-MM-DD), 못 찾으면 해당 건 자체를 저장하지 않음 |
  | url | 정규화된 원문 URL |
  | source_domain | 수집 도메인 |

  저장 위치는 `data/final/{lv2_id}/{type_name}.csv` (LV2·type 조합별 파일 1개).
- 외부 사용 가능 여부: TODO (라이선스/재배포 범위 확정 후 명시)

### 3. 구체적인 설명
1. 방법 1. 검색어 생성 (`src/query/generator.py`): taxonomy type의 정의/설명/include·exclude 기준을
   바탕으로 OpenAI로 Tavily/SerpAPI용 검색어를 미리 생성해 DB(`queries` 테이블)에 저장해둔다.
2. 방법 2. Discovery (`src/discovery/`): `scheduler.py`가 type/LV2별 목표 수집량에 맞춰 SerpAPI(도메인
   3개씩 묶은 `site:` 검색, `domain_bundling.py`)와 Tavily(도메인 블랙리스트 제외) 검색을 번갈아
   호출해 후보 URL을 쌓는다. 같은 검색어+도메인 조합(fingerprint)은 캐시해서 재호출하지 않는다.
3. 방법 3. Extraction (`src/extraction/`): `parser_registry.py`가 도메인별 전용 파서
   (`cook82_parser.py`, `dcinside_parser.py`, `ruliweb_parser.py`, `instiz_parser.py`, `kin_parser.py`)를
   우선 적용하고, 등록되지 않은 도메인은 `general_extractor.py`(trafilatura)로 처리한다. 제목이
   없거나 본문이 `min_content_length` 미만이면 실패 처리한다.
4. 방법 4. Filtering (`src/filtering/pipeline.py`): 비용이 싼 규칙 기반 필터(블랙리스트 → 중복 →
   기간)를 먼저 통과해야 비싼 LLM 기반 필터(한국어 비율 → OpenAI taxonomy 적합성 판단)를 호출한다.
   앞 단계에서 탈락하면 뒷 단계는 아예 실행하지 않는다(비용 절감).

### 4. 파이프라인 설명 및 실행

1. 파이프라인 설명 (Streamlit UI 기반, 별도 `.py`/`.sh` 배치 스크립트는 없음)
   - `app.py` 실행 시 5개 페이지가 순서대로 뜬다: (1) 수집 설정 — LV2/type·목표 수집량·기간 지정,
     (2) 도메인 설정 — `type_domains.yaml`의 SerpAPI 허용 도메인 편집, (3) 검색어 검토 — 자동 생성된
     검색어 확인/재생성, (4) 실행 및 결과 — `run_preflight()`로 사전 점검 후 `run_collection()`으로
     실제 API 호출·수집·필터링을 실행하고 결과를 `export_run()`으로 CSV에 반영, (5) 데이터 탐색 —
     누적된 `data/final/` CSV를 조회.
   - 실제 수집(API 호출)은 "4. 실행 및 결과" 화면에서 버튼을 눌러야만 시작된다 — 사전 점검
     단계에서 API 키 누락, 검색어 없음, 도메인 없음 등을 미리 경고해 크레딧 낭비를 막는다.

2. 파이프라인 실행

```bash
streamlit run app.py
```

   - 실행 전 `.env`에 `OPENAI_API_KEY`, `TAVILY_API_KEY`, `SERPAPI_KEY`를 채워야 한다(`.env.example` 참고).
   - UI에서 조정하는 주요 인자:
     - **목표 수집량 / candidate_multiplier**: `configs/collection.yaml`의 기본값을 화면에서 덮어씀
       (LV2 기준 목표를 하위 type이 나눠 가짐).
     - **수집 기간(date_from/date_to)**: 화면 1번에서 지정, 게시일 필터에 그대로 쓰임.
     - **taxonomy 적합성 LLM 필터 on/off**: 끄면 규칙 기반 필터만 통과해도 채택됨(속도/비용 우선 시).
     - **도메인 목록**: 화면 2번에서 type별 SerpAPI 허용 도메인을 추가/삭제 (`type_domains.yaml` 갱신).

### 5. 이슈
- 사이트 본문 추출 가능 여부는 반드시 실제 게시글 URL로 확인해야 한다 — 홈페이지/피드만 보고
  판단하면 SPA로 오판하기 쉽다(예: lawtalk.co.kr, velog.io는 목록/홈만 클라이언트 렌더링이고
  개별 게시글 상세는 서버 렌더링이라 정상 추출됨).
- `brunch.co.kr`은 익명 요청 시 카카오 로그인 화면으로 강제 리다이렉트되어 수집원에서 제외.
- 일부 사이트(`ppomppu.co.kr`, `bemil.chosun.com`)는 EUC-KR 인코딩을 쓰므로, UTF-8로 강제 디코딩하면
  본문이 깨진 것처럼 보인다 — 인코딩 자동 감지가 되는지 확인 필요.
- Playwright/Firecrawl 등 JS 렌더링 수단은 기본 off이므로, 정적 렌더링이 안 되는 도메인은 실질적으로
  수집 불가(SerpAPI 크레딧만 소모하고 저장 0건).
- `configs/type_domains.yaml`과 `configs/blacklist.yaml`은 서로 겹치는 도메인이 없어야 하며
  (`src/config/validator.py`의 `_validate_domain_overlap`으로 검증), 새 도메인 추가 전 robots.txt +
  실제 게시글 정적 렌더링 여부를 실측하고 반영해야 한다.

### 6. Requirements

```
pyyaml
python-dotenv
pytest
openai
streamlit
tavily-python
serpapi
requests
trafilatura
beautifulsoup4
```

```bash
pip install -r requirements.txt
```

### 7. 폴더 구조

```
.
├── app.py                          # Streamlit 진입점 (페이지 네비게이션만 담당)
├── requirements.txt
├── .env.example
├── configs/                        # 앱 전역/수집/추출/재시도/taxonomy 등 YAML 설정
│   ├── app.yaml
│   ├── blacklist.yaml
│   ├── collection.yaml
│   ├── domain_aliases.yaml
│   ├── extraction.yaml
│   ├── logging.yaml
│   ├── providers.yaml
│   ├── retry_policy.yaml
│   ├── taxonomy.yaml
│   └── type_domains.yaml
├── prompts/                        # OpenAI 프롬프트 템플릿
│   ├── korea_relevance.yaml
│   ├── query_generation.yaml
│   └── taxonomy_filtering.yaml
├── src/
│   ├── config/                     # 설정 로딩·검증
│   ├── query/                      # 검색어 생성/저장소
│   ├── discovery/                  # SerpAPI/Tavily 검색, 스케줄러, 도메인 번들링
│   ├── extraction/                 # 사이트별 파서 + 범용 추출기(trafilatura)
│   ├── filtering/                  # 블랙리스트/중복/기간/한국어 비율/taxonomy 필터 체인
│   ├── pipeline/                   # 후보 처리·사전 점검(preflight)·수집 실행
│   ├── storage/                    # SQLite 저장소, CSV export
│   └── utils/                      # URL 정규화, rate limit, 텍스트 유틸
├── ui/
│   ├── common.py
│   ├── components/
│   └── pages/                      # 1~5단계 Streamlit 페이지
├── database/                       # SQLite DB (content.db)
├── data/
│   ├── final/{LV2}/{type}.csv      # 최종 수집 결과
│   └── fail/{LV2}/{type}.csv       # 실패/샘플 데이터
└── tests/                          # pytest 단위 테스트
```
