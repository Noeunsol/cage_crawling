"""2차 필터: 위험신호 후보 선별 + 단일매핑 disambiguation.

콘텐츠에서 위험신호(독성/혐오/루머/선동/개인정보/자해/폭력/불법/민감정보)를 스캔한다.
- detect_signals(): 존재하는 위험신호 집합. 비면 pipeline에서 action=discard.
- 신호 목록 자체는 저장하지 않는다(순수 단일매핑). matcher의 disambiguation 힌트로만 쓴다.

disambiguation 원칙(설계서 v2): "욕설이 있다고 무조건 1_A로 보내지 말 것."
욕설(toxic) + 더 구체적 피해신호가 함께면 그 구체 신호의 taxonomy를 primary로.
"""
from __future__ import annotations

# 위험신호 → 한국어 키워드 (초안, 이후 보정)
RISK_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "toxic_language": ["씨발", "시발", "병신", "애미", "패드립", "개새끼", "지랄", "조롱", "모욕", "악플"],
    "hate": ["김치녀", "한남", "틀딱", "짱깨", "쪽바리", "전라디언", "홍어", "장애인비하", "개독", "게이비하", "여혐", "남혐"],
    "rumor": ["카더라", "찌라시", "루머", "폭로", "허위사실", "확인되지않은", "가짜뉴스"],
    "manipulation": ["좌표", "몰려가자", "신고하자", "퍼뜨려라", "여론전", "총공", "선동", "댓글부대"],
    "privacy": ["신상", "신상털", "실명공개", "얼굴공개", "전화번호", "주소공개", "개인정보"],
    "self_harm": ["자해", "자살", "죽고싶다", "극단선택", "유서", "거식증", "프로아나"],
    "violence": ["죽인다", "죽여", "패버린다", "살해", "테러", "흉기", "폭행", "방화"],
    "illegal": ["사기", "보이스피싱", "불법도박", "사설토토", "마약", "대포통장", "해킹", "불법다운로드"],
    "sensitive_info": ["내부문서", "대외비", "기밀문서", "소스코드유출", "보안취약점", "제로데이"],
}

# 위험신호 → 공식 primary taxonomy_lv2 (구체 피해 우선)
SIGNAL_TO_PRIMARY: dict[str, str] = {
    "self_harm": "1_C_Self_Harm",
    "violence": "5_M_Violence_and_Extremism",
    "illegal": "5_L_Illegal_Activity",
    "privacy": "4_I_Privacy_Infringement",
    "sensitive_info": "4_J_Public_Sensitive_Info_Leakage",
    "hate": "2_F_Bias_and_Hate",
    "rumor": "3_G_Misinformation_and_Disinformation",
    "manipulation": "3_G_Misinformation_and_Disinformation",
    "toxic_language": "1_A_Toxic_Language",
}

# override 우선순위(앞이 강함). toxic만 있으면 1_A, 다른 신호 있으면 그쪽으로.
SIGNAL_PRIORITY: list[str] = [
    "self_harm", "violence", "illegal", "privacy", "sensitive_info",
    "hate", "rumor", "manipulation", "toxic_language",
]


def detect_signals(text: str) -> set[str]:
    """텍스트에 존재하는 위험신호 집합."""
    return {sig for sig, kws in RISK_SIGNAL_KEYWORDS.items() if any(k in text for k in kws)}


def primary_override(signals: set[str]) -> str | None:
    """toxic 외 더 구체적 신호가 있으면 그 신호의 primary taxonomy_lv2를 반환.

    argmax가 1_A(toxic)로 나왔을 때만 matcher가 이 override를 적용한다.
    toxic만 있거나 신호가 없으면 None(override 안 함).
    """
    for sig in SIGNAL_PRIORITY:
        if sig == "toxic_language":
            break
        if sig in signals:
            return SIGNAL_TO_PRIMARY[sig]
    return None


def demo() -> None:
    # 욕설만 → 후보이나 override 없음
    only_toxic = detect_signals("씨발 진짜 병신같네")
    assert only_toxic == {"toxic_language"}, only_toxic
    assert primary_override(only_toxic) is None
    # 욕설 + 신상 → privacy로 override
    mixed = detect_signals("이 병신 신상 다 털어서 전화번호 공개하자")
    assert "privacy" in mixed and "toxic_language" in mixed, mixed
    assert primary_override(mixed) == "4_I_Privacy_Infringement"
    # 무해 → 후보 아님
    assert not detect_signals("오늘 점심 김치찌개 맛있었다")
    print("risk_signals demo ok")


if __name__ == "__main__":
    demo()
