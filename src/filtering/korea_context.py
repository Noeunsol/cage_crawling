"""본문의 한국 사건·영향·대응 맥락을 규칙 기반으로 점수화한다."""
from __future__ import annotations

import re
from urllib.parse import urlparse


_DOMESTIC_DOMAINS = {
    "go.kr", "or.kr", "ac.kr", "yna.co.kr", "newsis.com", "naver.com", "daum.net",
    "kbs.co.kr", "imbc.com", "sbs.co.kr", "jtbc.co.kr", "chosun.com", "donga.com",
    "hani.co.kr", "khan.co.kr", "mk.co.kr", "hankyung.com",
}
_LOCATIONS = (
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
    "충북", "충남", "전북", "전남", "경북", "경남", "제주", "대한민국", "국내",
    "한국", "우리나라", "월성", "고리", "한울", "한빛", "새울",
)
_INSTITUTIONS = (
    "정부", "경찰", "검찰", "소방", "질병관리청", "환경부", "원자력안전위원회", "원안위",
    "한국수력원자력", "한수원", "해양수산부", "식품의약품안전처", "식약처", "국립환경과학원",
    "지자체", "시청", "도청", "군청", "법원",
)
_IMPACT = (
    "피해", "영향", "우려", "오염", "유입", "노출", "누출", "유출", "확산", "도난",
    "검출", "위험", "안전", "수산업", "어민", "국민", "주민", "국내 해역", "국내 수산물",
)
_RESPONSE = (
    "조사", "수사", "검거", "기소", "판결", "대응", "방제", "측정", "검사", "감시",
    "발표", "처분", "대피", "통제", "회수",
)
_FOREIGN = ("후쿠시마", "일본", "미국", "중국", "러시아", "우크라이나", "유럽", "IAEA")


def _has_any(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term.lower() in text]


def _domestic_domain(url: str) -> bool:
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in _DOMESTIC_DOMAINS)


def score_korea_context(title: str, body: str, source_url: str = "") -> tuple[float, list[str]]:
    """한국 내 발생 또는 한국의 피해·영향·대응 근거를 점수와 함께 반환한다."""
    text = re.sub(r"\s+", " ", f"{title} {body}").lower()
    locations = _has_any(text, _LOCATIONS)
    institutions = _has_any(text, _INSTITUTIONS)
    impacts = _has_any(text, _IMPACT)
    responses = _has_any(text, _RESPONSE)
    foreign = _has_any(text, _FOREIGN)
    explicit_korea = any(term in text for term in ("한국", "대한민국", "국내", "우리나라"))

    score = 0.0
    evidence: list[str] = []
    if _domestic_domain(source_url):
        score += 0.10
        evidence.append("국내 출처")
    if locations:
        score += 0.40
        evidence.append(f"한국 지역·시설:{','.join(locations[:3])}")
    if institutions:
        score += 0.20
        evidence.append(f"한국 대응 주체:{','.join(institutions[:3])}")
    if explicit_korea and impacts:
        score += 0.25
        evidence.append(f"한국 영향:{','.join(impacts[:3])}")
    if explicit_korea and responses:
        score += 0.10
        evidence.append(f"한국 대응:{','.join(responses[:3])}")

    # 해외 사건을 한국어로 옮긴 것뿐이면 국내 출처 점수만으로 통과시키지 않는다.
    if foreign and not (explicit_korea and (impacts or responses)) and not institutions:
        score = min(score, 0.30)
        evidence.append(f"해외 사건:{','.join(foreign[:2])}")

    return round(min(score, 1.0), 3), evidence
