"""명확한 PII만 정규식으로 마스킹하는 1차 구현."""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

MASKING_VERSION = "v1_regex_basic"


@dataclass(frozen=True)
class MaskedEntity:
    entity_type: str
    original_hash: str
    start: int
    end: int
    replacement: str
    confidence: float


@dataclass
class MaskingResult:
    masked_text: str
    entities: list[MaskedEntity] = field(default_factory=list)
    pii_detected: bool = False
    pii_types: list[str] = field(default_factory=list)
    pii_risk_score: float = 0.0
    masking_version: str = MASKING_VERSION
    warnings: list[str] = field(default_factory=list)


class Masker(ABC):
    @abstractmethod
    def mask(self, text: str) -> MaskingResult:
        """원문 값은 보관하지 않고 마스킹 결과와 탐지 메타데이터를 반환한다."""


@dataclass(frozen=True)
class _Rule:
    key: str
    entity_type: str
    pattern: re.Pattern
    replacement: str
    confidence: float
    risk_weight: float


# 앞 규칙이 우선권을 갖는다. RRN/card/phone/account처럼 겹칠 수 있는 숫자 패턴 순서가 중요하다.
_RULES = [
    _Rule("credential", "SECRET",
          re.compile(r"(?i)\b(?:api[_ -]?key|access[_ -]?token|token|password|secret)\s*[:=]\s*['\"]?[^\s,'\"]{6,}"),
          "[SECRET]", 0.98, 0.5),
    _Rule("rrn", "RRN", re.compile(r"\b\d{6}-[1-8]\d{6}\b"), "[RRN]", 0.99, 0.5),
    _Rule("email", "EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[EMAIL]", 0.99, 0.2),
    _Rule("card", "CARD", re.compile(r"(?<!\d)(?:\d[ -]?){15}\d(?!\d)"), "[CARD]", 0.9, 0.35),
    _Rule("phone", "PHONE",
          re.compile(r"(?<!\d)(?:\+?82[- ]?)?0(?:2|1[016789]|[3-6]\d)[- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
          "[PHONE]", 0.98, 0.2),
    _Rule("address", "ADDRESS",
          re.compile(r"[가-힣]+(?:특별시|광역시|특별자치시|도)\s+[가-힣]+(?:시|군|구)\s+[가-힣0-9·.-]+(?:로|길)\s*\d+(?:-\d+)?"),
          "[ADDRESS]", 0.85, 0.2),
    _Rule("account_id", "ACCOUNT_ID",
          re.compile(r"(?i)\b(?:account(?:_?id)?|user(?:name|_?id)?|아이디|계정)\s*[:=]\s*@?[a-z0-9_.-]{3,32}\b"),
          "[ACCOUNT_ID]", 0.9, 0.2),
    _Rule("account_number", "ACCOUNT",
          re.compile(r"(?<!\d)\d{2,6}(?:-\d{2,6}){2,3}(?!\d)"),
          "[ACCOUNT]", 0.75, 0.3),
]
_RISK_BY_TYPE = {rule.entity_type: rule.risk_weight for rule in _RULES}


class BasicPIIMasker(Masker):
    def __init__(self, config: dict | None = None):
        config = config or {}
        self.enabled = config.get("enabled", True)
        self.version = config.get("masking_version", MASKING_VERSION)
        configured = config.get("mask_types", {})
        self.mask_types = {rule.key: configured.get(rule.key, True) for rule in _RULES}

    def mask(self, text: str, mask_types: dict[str, bool] | None = None) -> MaskingResult:
        if not self.enabled or not text:
            return MaskingResult(masked_text=text or "", masking_version=self.version)

        enabled = {**self.mask_types, **(mask_types or {})}
        accepted: list[tuple[int, int, _Rule, str]] = []
        warnings: list[str] = []
        for rule in _RULES:
            if not enabled.get(rule.key, True):
                continue
            for match in rule.pattern.finditer(text):
                start, end = match.span()
                if any(start < other_end and end > other_start
                       for other_start, other_end, _, _ in accepted):
                    warnings.append(f"overlap_skipped:{rule.entity_type}:{start}-{end}")
                    continue
                accepted.append((start, end, rule, match.group(0)))

        accepted.sort(key=lambda item: item[0])
        entities = [
            MaskedEntity(
                entity_type=rule.entity_type,
                original_hash=hashlib.sha256(original.encode("utf-8")).hexdigest(),
                start=start,
                end=end,
                replacement=rule.replacement,
                confidence=rule.confidence,
            )
            for start, end, rule, original in accepted
        ]

        masked = text
        for start, end, rule, _ in reversed(accepted):
            masked = masked[:start] + rule.replacement + masked[end:]

        types = list(dict.fromkeys(entity.entity_type for entity in entities))
        risk = risk_for_types(types)
        return MaskingResult(
            masked_text=masked,
            entities=entities,
            pii_detected=bool(entities),
            pii_types=types,
            pii_risk_score=round(risk, 3),
            masking_version=self.version,
            warnings=warnings,
        )


_DEFAULT_MASKER = BasicPIIMasker()


def mask_pii(text: str) -> str:
    """기존 호출부 호환용. 기본 PII와 credential을 모두 마스킹한다."""
    return _DEFAULT_MASKER.mask(text).masked_text


def apply_preservation_policy(text: str, policy: dict | None = None) -> str:
    """기존 taxonomy preservation policy를 마스킹 타입 설정으로 변환한다."""
    policy = policy or {}
    overrides = {}
    if not policy.get("mask_pii", True):
        overrides.update({rule.key: False for rule in _RULES if rule.key != "credential"})
    if not policy.get("mask_credentials", True):
        overrides["credential"] = False
    return _DEFAULT_MASKER.mask(text, overrides).masked_text


def pii_risk(text: str) -> float:
    return _DEFAULT_MASKER.mask(text).pii_risk_score


def risk_for_types(pii_types: list[str]) -> float:
    """탐지 건수가 아니라 고유 PII 유형별 가중치를 합산한다."""
    return round(min(sum(_RISK_BY_TYPE.get(entity_type, 0.0)
                         for entity_type in set(pii_types)), 1.0), 3)
