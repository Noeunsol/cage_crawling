from src.filtering.korea_context import score_korea_context


def test_foreign_event_with_korean_impact_scores_as_korean_context():
    score, evidence = score_korea_context(
        "후쿠시마 오염수, 국내 수산업 피해 우려",
        "한국 정부와 해양수산부는 국내 해역과 수산물 검사를 강화한다고 발표했다.",
        "https://www.yna.co.kr/view/example",
    )
    assert score >= 0.7
    assert any(item.startswith("한국 영향:") for item in evidence)


def test_korean_translation_of_foreign_event_is_not_korean_context():
    score, _ = score_korea_context(
        "후쿠시마 원전 사고 경과",
        "일본 후쿠시마에서 발생한 원전 사고의 역사와 현지 상황을 설명한다.",
        "https://example.com/article",
    )
    assert score < 0.4


def test_domestic_cbrne_incident_scores_as_korean_context():
    score, evidence = score_korea_context(
        "아산 도로에서 유해화학물질 유출",
        "충남 아산에서 화물차가 전도돼 소방당국과 환경부가 방제와 조사를 진행했다.",
        "https://www.newsis.com/view/example",
    )
    assert score >= 0.7
    assert evidence
