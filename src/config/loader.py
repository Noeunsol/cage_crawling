"""configs/*.yaml 을 읽어 하나의 딕셔너리로 모아주는 로더.

설계 원칙 (CLAUDE_CRAWLER_IMPLEMENTATION_PLAN.md 2.1절):
  - 기본값/타임아웃/경로/비율 등 바뀔 수 있는 값은 전부 YAML에만 있고 코드에는 없다.
  - API key/비밀값은 YAML에 두지 않고 환경변수 이름만 참조한다 (.env는 dotenv로 읽는다).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"

# configs/ 아래에 있어야 하는 필수 파일 목록. 하나라도 없으면 앱을 시작할 수 없다.
REQUIRED_CONFIG_FILES = [
    "app",
    "collection",
    "providers",
    "extraction",
    "retry_policy",
    "taxonomy",
    "type_domains",
    "domain_aliases",
    "blacklist",
    "logging",
]


def load_yaml(path: Path) -> dict:
    """YAML 파일 하나를 읽어 dict로 반환한다. 빈 파일은 빈 dict로 취급한다."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_all_configs(config_dir: Path = CONFIG_DIR) -> dict[str, dict]:
    """configs/{name}.yaml 을 전부 읽어 {name: 내용} 형태로 반환한다.

    반환된 dict는 이후 validator.validate_configs()로 검증한 뒤 사용해야 한다.
    """
    # override=True: 셸에 이미 같은 이름의 (비어 있거나 오래된) 환경변수가 있어도 .env 값을 우선한다.
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    configs: dict[str, dict] = {}
    for name in REQUIRED_CONFIG_FILES:
        path = config_dir / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"필수 설정 파일이 없습니다: {path} "
                f"(configs/ 아래에 {name}.yaml 을 추가해주세요)"
            )
        configs[name] = load_yaml(path)
    return configs
