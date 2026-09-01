"""SQLite 연결과 스키마 초기화.

기존 DB 파일이 있으면 절대 지우지 않는다.
schema.sql은 전부 CREATE ... IF NOT EXISTS라서 몇 번을 불러도 데이터가 유지된다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# CREATE TABLE IF NOT EXISTS는 이미 있는 테이블에 새 컬럼을 추가해주지 않는다 — 기존 테이블에
# 컬럼이 새로 필요해지면 여기 추가한다 (2026-08-31: 근사 중복 탐지용 컬럼). 이미 있으면 조용히
# 건너뛴다 — 매번 connect()할 때마다 안전하게 다시 돌 수 있다.
_COLUMN_MIGRATIONS = [
    ("query_generation_calls", "run_id", "TEXT REFERENCES collection_runs(run_id)"),
    ("query_generation_calls", "web_search_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("contents", "title_normalized", "TEXT"),
    ("contents", "content_fingerprint", "TEXT"),
    ("content_duplicates", "title_similarity", "REAL"),
    ("content_duplicates", "content_similarity", "REAL"),
]

# informativeness -> content_quality 컬럼명 변경 (2026-08-31: 정량평가 품질 지표를 스펙의
# 4항목-한국관련성/Taxonomy관련성/사례구체성/본문품질-에 맞추면서). RENAME COLUMN은 SQLite
# 3.25+에서 지원하며 기존 값(과거 정의 기준 점수)은 그대로 보존된다.
_COLUMN_RENAMES = [
    ("content_quality_scores", "informativeness", "content_quality"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, col_type in _COLUMN_MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    for table, old_name, new_name in _COLUMN_RENAMES:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if old_name in existing and new_name not in existing:
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")


def connect(db_path: Path) -> sqlite3.Connection:
    """DB에 연결하고 스키마를 보장한 뒤 Connection을 반환한다.

    row_factory=sqlite3.Row로 컬럼명으로 접근 가능하게 하고, FK 제약을 켠다.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: 이 커넥션을 만든 스레드가 아닌 곳에서 써도 되게 한다
    # (ui/common.py의 get_db()가 세션별로 커넥션을 하나씩 만들어 쓰므로, 커넥션 자체를
    # 여러 스레드가 "동시에" 쓰는 상황은 이제 없다 — 세션마다 별도 객체라 순차 실행됨).
    # timeout=30: 서로 다른 세션(=별도 커넥션)이 거의 같은 순간에 쓰기를 시도하면 SQLite
    # 파일 잠금에 걸릴 수 있는데, 바로 에러 내지 않고 최대 30초 기다렸다가 재시도한다.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _apply_column_migrations(conn)
    conn.commit()
    return conn
