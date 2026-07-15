"""진입점. python -m src.main [--config ...]"""
from __future__ import annotations

import argparse
import logging

from . import pipeline


def main() -> None:
    p = argparse.ArgumentParser(description="Taxonomy 기반 한국 웹 크롤러 (mock end-to-end skeleton)")
    p.add_argument("--config", default="configs/taxonomy_policy.yaml", help="taxonomy policy YAML")
    p.add_argument("--sites", default="configs/site_policy.yaml", help="site policy YAML")
    p.add_argument("--settings", default="configs/crawler_settings.yaml", help="crawler settings YAML")
    p.add_argument("--db", default="data/content.db", help="sqlite DB 경로")
    p.add_argument("--report", default="data/exports/report.json", help="리포트 JSON 경로")
    p.add_argument("--csv", default="data/exports/content.csv", help="content_records CSV 경로")
    p.add_argument("--after", default=None, help="수집 시작일 YYYY-MM-DD (기본: settings의 date_range)")
    p.add_argument("--before", default=None, help="수집 종료일 YYYY-MM-DD (기본: settings의 date_range)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    pipeline.run(
        taxonomy_config=args.config,
        site_config=args.sites,
        settings_config=args.settings,
        db_path=args.db,
        report_path=args.report,
        csv_path=args.csv,
        after=args.after,
        before=args.before,
    )


if __name__ == "__main__":
    main()
