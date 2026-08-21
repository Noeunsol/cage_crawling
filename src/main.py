"""CLI 진입점. `python -m src.main --mode {trend,targeted}`

1차(trend)와 2차(targeted)는 구현이 완전히 다르다 — 여기서는 인자만 받아
src/phase1, src/phase2로 각각 위임하고 공통 로직을 두지 않는다.
"""
from __future__ import annotations

import argparse
import logging

from src.common import paths
from src.common.logging_setup import setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description="Taxonomy 기반 한국 웹 크롤러")
    p.add_argument("--mode", choices=["trend", "targeted"], default="trend",
                   help="trend=1차 트렌드 수집, targeted=2차 부족 taxonomy 보강")
    p.add_argument("--taxonomy", default=paths.TAXONOMY_CONFIG, help="분류 taxonomy YAML (단일 정본)")
    p.add_argument("--sites", default=paths.SITE_CONFIG, help="site policy YAML")
    p.add_argument("--settings", default=paths.SETTINGS_CONFIG, help="공용 crawler settings YAML")
    p.add_argument("--trend-config", default=paths.TREND_CONFIG, help="1차 수집 설정 YAML")
    p.add_argument("--phase2-config", default=paths.PHASE2_CONFIG, help="2차 수집 설정 YAML")
    p.add_argument("--db", default=paths.DB_DEFAULT, help="sqlite DB 경로")
    p.add_argument("--report", default=paths.REPORT_DEFAULT, help="리포트 JSON 경로")
    p.add_argument("--csv", default=paths.CSV_DEFAULT, help="content_records CSV 경로 (1차)")
    p.add_argument("--dry-run", action="store_true", help="fetch/추출 없이 수집 예정 범위만 프리뷰")
    p.add_argument("--reset-db", action="store_true", help="기존 DB 삭제 후 재생성")
    p.add_argument("--sample-review", default=None, metavar="LV2",
                   help="2차: 저장된 LV2 표본을 수동 검수용 CSV로 내보내고 종료")
    p.add_argument("--sample-n", type=int, default=20, help="--sample-review 표본 수")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(level=logging.INFO if args.verbose else logging.WARNING, component="cli")

    if args.sample_review:
        from src.phase2.review import sample_for_review
        out = paths.review_csv(args.sample_review)
        rows = sample_for_review(args.db, args.sample_review, args.sample_n, out_path=out)
        print(f"표본 {len(rows)}건 → {out}")
        print("합격선: domestic_direct ≥95%, LV2 ≥90%, combined ≥85%")
        return

    if args.mode == "targeted":
        from src.phase2.run import run_targeted
        run_targeted(
            config=args.phase2_config,
            taxonomy_config=args.taxonomy,
            site_config=args.sites,
            settings_config=args.settings,
            db_path=args.db,
            report_path=args.report,
            dry_run=args.dry_run,
        )
        return

    from src.phase1.run import run_trend
    run_trend(
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


if __name__ == "__main__":
    main()
