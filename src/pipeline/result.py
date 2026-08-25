"""한 번의 수집 실행 결과를 담는 자료구조 (14.6절 결과 화면이 그대로 보여줄 값들)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProcessOutcome:
    """process_candidate() 하나의 결과."""

    status: str            # accepted / excluded / discarded
    reason: str | None = None    # status != accepted일 때 retry_policy.yaml의 reason code
    detail: str | None = None    # 사람이 읽을 설명
    openai_usage: dict | None = None  # taxonomy_filter가 실제로 호출됐으면 {"prompt_tokens", "completion_tokens", "elapsed_s"}


@dataclass
class ProgressEvent:
    """후보 하나를 처리할 때마다 UI에 보고하는 진행 상황 (14.5절)."""

    lv2_id: str
    type_name: str
    url: str
    outcome: ProcessOutcome
    processed: int          # 지금까지 처리한 후보 수
    total: int               # 이번 실행에서 찾은 후보 총 수


@dataclass
class RunSummary:
    run_id: str
    candidates_found: int = 0
    accepted: int = 0
    excluded: int = 0
    discarded: int = 0
    discard_reasons: dict[str, int] = field(default_factory=dict)   # reason -> count
    provider_usage: dict = field(default_factory=dict)               # provider -> [usage, ...]
    warnings: list[str] = field(default_factory=list)

    def record(self, outcome: ProcessOutcome) -> None:
        if outcome.status == "accepted":
            self.accepted += 1
        elif outcome.status == "excluded":
            self.excluded += 1
        else:
            self.discarded += 1
        if outcome.reason:
            self.discard_reasons[outcome.reason] = self.discard_reasons.get(outcome.reason, 0) + 1
        if outcome.openai_usage:
            # tavily/serpapi와 같은 모양(provider -> [usage, ...])으로 넣어서 결과 화면의
            # "provider별 호출 수" 표에 openai(taxonomy_filter) 호출도 그대로 같이 뜨게 한다.
            self.provider_usage.setdefault("openai", []).append(outcome.openai_usage)


def summarize(run_id: str, events: list[ProgressEvent], *, provider_usage: dict, warnings: list[str]) -> RunSummary:
    summary = RunSummary(
        run_id=run_id, candidates_found=len(events), provider_usage=provider_usage, warnings=list(warnings),
    )
    for event in events:
        summary.record(event.outcome)
    return summary
