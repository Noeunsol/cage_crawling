-- SQLite 스키마 (12.2절). 타임스탬프는 SQLite의 strftime으로 기본값을 채워
-- Python 쪽에서 매번 datetime을 넘길 필요가 없게 했다.
-- CREATE TABLE/INDEX 모두 IF NOT EXISTS라 여러 번 실행해도 안전하다 (idempotent).

PRAGMA foreign_keys = ON;

-- 한 번의 수집 실행(run) 단위. 실행 설정 스냅샷과 최종 provider 사용량을 담는다.
CREATE TABLE IF NOT EXISTS collection_runs (
    id                     INTEGER PRIMARY KEY,
    run_id                 TEXT NOT NULL UNIQUE,
    started_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at            TEXT,
    settings_snapshot      TEXT NOT NULL,   -- JSON: target_count, date range, provider ratio 등
    status                 TEXT NOT NULL,   -- running / completed / stopped / failed
    provider_usage_summary TEXT,            -- JSON: {"tavily": {...}, "serpapi": {...}}
    warning_summary        TEXT             -- JSON: 도메인 누락, 목표 초과 등 경고 목록
);

-- 최종 저장되는 콘텐츠. accepted/excluded/failed 상태를 모두 포함한다 (11.3절).
CREATE TABLE IF NOT EXISTS contents (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL,
    published_date      TEXT,               -- YYYY-MM-DD, 못 찾으면 NULL
    canonical_url       TEXT NOT NULL UNIQUE,
    source_name         TEXT,
    source_domain       TEXT NOT NULL,
    source_category     TEXT,               -- news / community / qna / blog ...
    status              TEXT NOT NULL,      -- accepted / excluded / failed / pending / processing
    first_discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_discovered_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    collected_at        TEXT,
    content_hash        TEXT NOT NULL,
    title_normalized    TEXT,               -- 근사 중복 탐지용: 태그/언론사명/반복특수문자 제거한 제목
    content_fingerprint TEXT                -- 근사 중복 탐지용: 본문 고유 단어 집합(공백 구분 문자열) — 10.3절 확장
);
CREATE INDEX IF NOT EXISTS idx_contents_status ON contents(status);
CREATE INDEX IF NOT EXISTS idx_contents_content_hash ON contents(content_hash);
CREATE INDEX IF NOT EXISTS idx_contents_source_domain ON contents(source_domain);

-- 하나의 콘텐츠가 여러 LV2/type과 연결될 수 있다 (12.1절).
CREATE TABLE IF NOT EXISTS content_taxonomy_mappings (
    id              INTEGER PRIMARY KEY,
    content_id      INTEGER NOT NULL REFERENCES contents(id),
    taxonomy_lv2    TEXT NOT NULL,
    type_name       TEXT NOT NULL,
    decision        TEXT NOT NULL,          -- accepted / excluded
    decision_reason TEXT,
    prompt_name     TEXT,
    prompt_version  TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (content_id, taxonomy_lv2, type_name)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_mappings_lv2_type
    ON content_taxonomy_mappings(taxonomy_lv2, type_name);

-- OpenAI가 만들거나 사용자가 추가/수정한 검색어 (5.4절: provider별로 완전히 분리된 이력).
CREATE TABLE IF NOT EXISTS search_queries (
    id              INTEGER PRIMARY KEY,
    taxonomy_lv2    TEXT NOT NULL,
    type_name       TEXT NOT NULL,
    provider        TEXT NOT NULL,          -- tavily / serpapi
    query_text      TEXT NOT NULL,
    status          TEXT NOT NULL,          -- generated / user_edited / user_created / rejected / unused / used
    parent_query_id INTEGER REFERENCES search_queries(id),  -- 수정 전 원본 쿼리 (이력 보존용)
    created_by      TEXT NOT NULL,          -- openai / user
    prompt_version  TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (provider, taxonomy_lv2, type_name, query_text)
);
CREATE INDEX IF NOT EXISTS idx_search_queries_lookup
    ON search_queries(taxonomy_lv2, type_name, provider, status);

-- 검색어가 실제로 API에 호출된 기록. request_fingerprint로 동일 조건 재실행을 막는다 (10.1절).
CREATE TABLE IF NOT EXISTS query_executions (
    id                  INTEGER PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES collection_runs(run_id),
    query_id            INTEGER NOT NULL REFERENCES search_queries(id),
    request_params      TEXT NOT NULL,      -- JSON: provider에 실제로 보낸 파라미터
    request_fingerprint TEXT NOT NULL UNIQUE,
    result_count        INTEGER,
    credit_usage        TEXT,               -- JSON: provider가 응답한 usage 정보
    status              TEXT NOT NULL,      -- success / error
    error_message       TEXT,
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_executions_run ON query_executions(run_id);
-- scoring.py::query_score()가 query_id 단독으로 조회한다 (2026-08-31) — run_id가 없으면
-- idx_query_executions_run을 못 써서 이게 없으면 전체 스캔이 된다.
CREATE INDEX IF NOT EXISTS idx_query_executions_query ON query_executions(query_id);

-- 검색어 생성(OpenAI) 호출 1건당 1행. DB 전체 OpenAI 비용 집계용 (14.3절 검색어 생성 화면).
CREATE TABLE IF NOT EXISTS query_generation_calls (
    id                 INTEGER PRIMARY KEY,
    taxonomy_lv2       TEXT NOT NULL,
    type_name          TEXT NOT NULL,
    provider           TEXT NOT NULL,          -- tavily / serpapi (이 호출로 검색어를 만든 대상)
    model              TEXT NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    elapsed_s          REAL NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 웹서치로 얻은 type별 최근 표현(fresh vocabulary) 캐시. TTL은 코드(fresh_vocabulary 레포)에서 판단.
CREATE TABLE IF NOT EXISTS fresh_vocabulary_cache (
    taxonomy_lv2  TEXT NOT NULL,
    type_name     TEXT NOT NULL,
    terms         TEXT NOT NULL,  -- JSON 문자열 배열
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (taxonomy_lv2, type_name)
);

-- 검색 결과로 어떤 콘텐츠가 어떤 실행/쿼리에서 발견됐는지 (provenance).
CREATE TABLE IF NOT EXISTS content_discoveries (
    id              INTEGER PRIMARY KEY,
    content_id      INTEGER NOT NULL REFERENCES contents(id),
    run_id          TEXT NOT NULL REFERENCES collection_runs(run_id),
    query_id        INTEGER NOT NULL REFERENCES search_queries(id),
    provider        TEXT NOT NULL,
    returned_url    TEXT NOT NULL,
    rank            INTEGER,
    relevance_score REAL,
    discovered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_content_discoveries_run_query
    ON content_discoveries(run_id, query_id);
-- scoring.py::query_score()가 query_id 단독으로 조회한다 (2026-08-31) — 위 복합 인덱스는
-- run_id가 선행 컬럼이라 run_id 없이 query_id만 필터링할 땐 못 쓴다.
CREATE INDEX IF NOT EXISTS idx_content_discoveries_query ON content_discoveries(query_id);

-- 저장하지 않기로 한(제외/실패) 후보. 본문은 담지 않는다 (11.4절).
CREATE TABLE IF NOT EXISTS discarded_candidates (
    id                 INTEGER PRIMARY KEY,
    original_url       TEXT NOT NULL,
    normalized_url     TEXT NOT NULL,
    run_id             TEXT REFERENCES collection_runs(run_id),
    query_id           INTEGER REFERENCES search_queries(id),
    source_domain      TEXT,
    reason             TEXT NOT NULL,       -- retry_policy.yaml의 reason code
    retryable          INTEGER NOT NULL,    -- 0 / 1
    requested_date_from TEXT,
    requested_date_to   TEXT,
    discarded_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_discarded_normalized_url ON discarded_candidates(normalized_url);
CREATE INDEX IF NOT EXISTS idx_discarded_run ON discarded_candidates(run_id);
-- retry_candidates.py::find_retryable_candidates()의 query_id 조회, scoring.py::domain_score()의
-- source_domain 조회용 (2026-08-31) — 둘 다 인덱스가 없어 전체 스캔이었다.
CREATE INDEX IF NOT EXISTS idx_discarded_query ON discarded_candidates(query_id);
CREATE INDEX IF NOT EXISTS idx_discarded_source_domain ON discarded_candidates(source_domain);

-- URL은 다르지만 같은 본문/사건으로 판단된 콘텐츠 (10.3절).
CREATE TABLE IF NOT EXISTS content_duplicates (
    id                       INTEGER PRIMARY KEY,
    representative_content_id INTEGER NOT NULL REFERENCES contents(id),
    duplicate_content_id     INTEGER REFERENCES contents(id),
    duplicate_url            TEXT,
    duplicate_reason         TEXT NOT NULL,  -- same_url / same_content_hash / same_event / near_duplicate
    title_similarity         REAL,           -- near_duplicate일 때만 채움 (10.3절 확장)
    content_similarity       REAL,           -- near_duplicate일 때만 채움
    detected_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 근사중복 shadow mode 관측 기록 (10.3절 확장, 2026-08-31). 임계값이 아직 검증 전이라 이걸로
-- 걸러내지(discard) 않고, 판단 근거(유사도 점수)만 쌓아서 나중에 사람이 라벨링해 임계값을
-- 확정하는 데 쓴다. would_exclude는 "지금 enforce 임계값이었다면 걸렀을지" 참고용.
CREATE TABLE IF NOT EXISTS near_duplicate_observations (
    id                  INTEGER PRIMARY KEY,
    content_id          INTEGER NOT NULL REFERENCES contents(id),
    matched_content_id  INTEGER NOT NULL REFERENCES contents(id),
    title_similarity    REAL NOT NULL,
    content_similarity  REAL NOT NULL,
    would_exclude       INTEGER NOT NULL,
    human_label         TEXT,    -- duplicate / not_duplicate — 라벨링 전엔 NULL (11.4절 eval_labels와 같은 패턴)
    observed_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_near_dup_obs_unlabeled ON near_duplicate_observations(human_label);

-- type당 SerpAPI 도메인 번들(최대 3개씩 묶음)의 활성 상태와 LRU 순환용 마지막 사용 시각.
-- configs/type_domains.yaml이 바뀌면 storage/repositories/domain_bundles.sync_bundles()가 다시 맞춘다.
CREATE TABLE IF NOT EXISTS serpapi_domain_bundles (
    id           INTEGER PRIMARY KEY,
    type_name    TEXT NOT NULL,
    bundle_index INTEGER NOT NULL,
    domains      TEXT NOT NULL,      -- JSON 배열
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_used_at TEXT,
    UNIQUE (type_name, bundle_index)
);
CREATE INDEX IF NOT EXISTS idx_domain_bundles_type ON serpapi_domain_bundles(type_name);

-- 정량 평가 실험용: accepted 콘텐츠에 대한 사람 라벨(Taxonomy 정밀도 산출용).
-- 정량 평가 실험용: accepted 콘텐츠의 OpenAI 품질 점수 (제외/재판정에는 쓰지 않는다 — 성능 수치화 전용).
CREATE TABLE IF NOT EXISTS content_quality_scores (
    content_id          INTEGER NOT NULL REFERENCES contents(id),
    taxonomy_lv2        TEXT NOT NULL,
    type_name           TEXT NOT NULL,
    specificity         INTEGER NOT NULL,
    informativeness     INTEGER NOT NULL,
    relevance_strength  INTEGER NOT NULL,
    korean_locality     INTEGER NOT NULL,
    overall             INTEGER NOT NULL,
    reason              TEXT,
    issues              TEXT,   -- JSON 문자열 배열 (예: ["ad_content", "generic_description"])
    model               TEXT NOT NULL,
    prompt_tokens        INTEGER NOT NULL,
    completion_tokens    INTEGER NOT NULL,
    elapsed_s            REAL NOT NULL,
    scored_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (content_id, taxonomy_lv2, type_name)
);

CREATE TABLE IF NOT EXISTS eval_labels (
    content_id   INTEGER NOT NULL REFERENCES contents(id),
    taxonomy_lv2 TEXT NOT NULL,
    type_name    TEXT NOT NULL,
    human_label  TEXT NOT NULL,   -- accepted / excluded (사람 판단)
    labeled_by   TEXT NOT NULL,
    labeled_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (content_id, taxonomy_lv2, type_name)
);
