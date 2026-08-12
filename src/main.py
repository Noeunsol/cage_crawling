"""진입점. python -m src.main [--config ...]"""
from __future__ import annotations

import argparse
import logging

from . import pipeline
from .logging_setup import setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description="Taxonomy 기반 한국 웹 크롤러")
    p.add_argument("--mode", choices=["keyword", "trend", "targeted"], default="keyword",
                   help="keyword=taxonomy 검색(레거시), trend=트렌드 수집, targeted=2차 semantic 보강")
    p.add_argument("--trend-config", default="configs/trend_collection.yaml", help="trend 모드 수집 설정 YAML")
    p.add_argument("--phase2-config", default="configs/targeted_collection.yaml",
                   help="targeted(2차) 모드 설정 YAML")
    p.add_argument("--taxonomy", default="configs/taxonomy.yaml", help="trend 모드 분류 taxonomy YAML")
    p.add_argument("--config", default="configs/taxonomy.yaml", help="taxonomy YAML (--taxonomy와 동일 정본)")
    p.add_argument("--sites", default="configs/site_policy.yaml", help="site policy YAML")
    p.add_argument("--settings", default="configs/crawler_settings.yaml", help="crawler settings YAML")
    p.add_argument("--db", default="data/db/content.db", help="sqlite DB 경로")
    p.add_argument("--report", default="data/exports/report.json", help="리포트 JSON 경로")
    p.add_argument("--csv", default="data/exports/content.csv", help="content_records CSV 경로")
    p.add_argument("--after", default=None, help="수집 시작일 YYYY-MM-DD (기본: settings의 date_range)")
    p.add_argument("--before", default=None, help="수집 종료일 YYYY-MM-DD (기본: settings의 date_range)")
    p.add_argument("--dry-run", action="store_true", help="fetch/추출 없이 수집 예정 범위만 프리뷰")
    p.add_argument("--reset-db", action="store_true", help="기존 DB 삭제 후 재생성")
    p.add_argument("--max-queries", type=int, default=None, help="이번 실행 SerpAPI 검색 상한 (settings 덮어씀)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(level=logging.INFO if args.verbose else logging.WARNING, component="cli")
    if args.mode == "targeted":
        pipeline.run_targeted(
            config=args.phase2_config,
            taxonomy_config=args.taxonomy,
            site_config=args.sites,
            settings_config=args.settings,
            db_path=args.db,
            report_path=args.report,
            dry_run=args.dry_run,
        )
        return
    if args.mode == "trend":
        pipeline.run_trend(
            trend_config=args.trend_config,
            taxonomy_config=args.taxonomy,
            site_config=args.sites,
            settings_config=args.settings,
            db_path=args.db,
            report_path=args.report,
            csv_path=args.csv,
            dry_run=args.dry_run,
            reset_db=args.reset_db,
        )
        return
    pipeline.run(
        taxonomy_config=args.config,
        site_config=args.sites,
        settings_config=args.settings,
        db_path=args.db,
        report_path=args.report,
        csv_path=args.csv,
        after=args.after,
        before=args.before,
        dry_run=args.dry_run,
        reset_db=args.reset_db,
        max_queries=args.max_queries,
    )


if __name__ == "__main__":
    main()
