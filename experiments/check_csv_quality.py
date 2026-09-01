"""CSV 콘텐츠 품질 채점. 예: python -m experiments.check_csv_quality CSV --limit 3 --confirm"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.config.loader import load_all_configs
from src.quality_check import score_csv, write_scores


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        print("OpenAI 호출 비용이 발생합니다. 실행하려면 --confirm을 붙이세요.")
        return

    output = args.output or args.input.with_name(f"{args.input.stem}_quality.csv")
    rows = score_csv(args.input, load_all_configs(), args.limit, lambda n, total: print(f"  {n}/{total} 채점 완료"))
    write_scores(rows, output)
    print(f"{len(rows)}건 결과를 {output}에 저장했습니다.")


if __name__ == "__main__":
    main()
