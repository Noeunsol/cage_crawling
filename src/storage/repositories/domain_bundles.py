"""type별 SerpAPI 도메인 번들의 활성 상태와 LRU 순환용 마지막 사용 시각 저장."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from src.discovery.domain_bundling import build_bundles


@dataclass
class DomainBundle:
    type_name: str
    bundle_index: int
    domains: list[str]


def sync_bundles(
    conn: sqlite3.Connection, type_name: str, domains: list[str], alias_groups: dict[str, list[str]],
) -> None:
    """configs/type_domains.yaml 기준으로 이 type의 번들 행을 최신 상태로 맞춘다.

    번들 구성이 바뀌어도 같은 index는 UPDATE라 last_used_at(LRU 이력)이 보존된다.
    더 이상 필요 없는 인덱스는 지운다. 여러 번 실행해도 안전하다(idempotent).
    """
    bundles = build_bundles(domains, alias_groups)
    with conn:
        for index, bundle_domains in enumerate(bundles):
            conn.execute(
                """
                INSERT INTO serpapi_domain_bundles (type_name, bundle_index, domains)
                VALUES (?, ?, ?)
                ON CONFLICT (type_name, bundle_index) DO UPDATE SET domains = excluded.domains
                """,
                (type_name, index, json.dumps(bundle_domains, ensure_ascii=False)),
            )
        conn.execute(
            "DELETE FROM serpapi_domain_bundles WHERE type_name = ? AND bundle_index >= ?",
            (type_name, len(bundles)),
        )


def has_enabled_bundle(conn: sqlite3.Connection, type_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM serpapi_domain_bundles WHERE type_name = ? AND enabled = 1 LIMIT 1",
        (type_name,),
    ).fetchone()
    return row is not None


def pick_bundle(conn: sqlite3.Connection, type_name: str) -> DomainBundle | None:
    """가장 오래 전에 쓰였거나(또는 한 번도 안 쓰인) 활성 번들을 하나 고른다 (LRU)."""
    row = conn.execute(
        """
        SELECT bundle_index, domains FROM serpapi_domain_bundles
        WHERE type_name = ? AND enabled = 1
        ORDER BY (last_used_at IS NULL) DESC, last_used_at ASC, bundle_index ASC
        LIMIT 1
        """,
        (type_name,),
    ).fetchone()
    if row is None:
        return None
    return DomainBundle(
        type_name=type_name, bundle_index=row["bundle_index"], domains=json.loads(row["domains"]),
    )


def mark_used(conn: sqlite3.Connection, type_name: str, bundle_index: int) -> None:
    with conn:
        conn.execute(
            """
            UPDATE serpapi_domain_bundles SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE type_name = ? AND bundle_index = ?
            """,
            (type_name, bundle_index),
        )


def set_enabled(conn: sqlite3.Connection, type_name: str, bundle_index: int, enabled: bool) -> None:
    with conn:
        conn.execute(
            "UPDATE serpapi_domain_bundles SET enabled = ? WHERE type_name = ? AND bundle_index = ?",
            (int(enabled), type_name, bundle_index),
        )


def list_bundles(conn: sqlite3.Connection, type_name: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM serpapi_domain_bundles WHERE type_name = ? ORDER BY bundle_index", (type_name,),
    ).fetchall()
