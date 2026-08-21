"""위험신호 어휘 단일 정본 — 신호명 → 한국어 표현, 신호 → taxonomy_lv2, 심각도 등급.

v23 이전에는 이 파일과 relevance_filter.py가 각자 RISK_SIGNAL_KEYWORDS를 들고 있었고
키 이름까지 갈라져 있었다(rumor vs rumor_or_misinformation, illegal vs illegal_activity).
1차 gate와 2차 acceptance가 서로 다른 어휘로 같은 신호를 찾던 상태라 한 곳으로 합쳤다.

쓰는 곳
  1차: relevance_filter가 제목·본문 gate와 risk_score/secondary_flags에 쓴다.
  2차: acceptance가 전략의 lv2_risk_signals로 지목한 신호의 어휘를 LV2 근거로 쓴다.
"""
from __future__ import annotations

# 위험신호 → 한국어 표현. 1차 gate와 2차 acceptance가 같은 어휘를 본다.
RISK_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    "toxic_language": ["씨발", "시발", "병신", "애미", "패드립", "개새끼", "개새", "지랄", "조롱",
                       "모욕", "악플", "개쓰레기", "꺼져", "미친", "쓰레기", "극혐", "비하"],
    "hate": ["김치녀", "한남", "한녀", "맘충", "틀딱", "짱깨", "쪽바리", "조센", "전라도", "전라디언",
             "홍어", "장애인비하", "장애인 비하", "개독", "게이비하", "여혐", "남혐", "외노자",
             "페미충", "성별 집단"],
    "harassment": ["근황", "저격", "박제", "사과문", "좌표", "털자", "조리돌림", "모욕", "협박",
                   "암살단", "응징"],
    "rumor_or_misinformation": ["카더라", "찌라시", "루머", "폭로", "논란", "실체", "밝혀짐", "조작",
                                "선동", "허위사실", "확인되지않은", "가짜뉴스", "몰려가자", "신고하자",
                                "퍼뜨려라", "여론전", "총공", "댓글부대"],
    "privacy": ["신상", "신상털", "실명", "실명공개", "얼굴공개", "얼굴", "주소", "주소공개", "주소유출",
                "전화번호", "전화번호유출", "계정", "인스타", "학교", "직장", "개인정보", "유출",
                "사진유출", "사진 유출"],
    "self_harm": ["자해", "자살", "죽고싶다", "죽고 싶", "죽고싶", "극단선택", "극단적 선택",
                  "목숨 끊", "유서", "거식증", "프로아나"],
    "sexual": ["성희롱", "성매매", "몰카", "야짤", "몸캠", "성추행", "자궁", "딥페이크", "합성물",
               "성착취", "지인능욕", "ncii"],
    "violence": ["죽인다", "죽여", "패야", "패버린다", "살해", "테러", "응징", "흉기", "칼부림",
                 "폭행", "방화", "살인", "암살", "폭발물"],
    "illegal_activity": ["사기", "보이스피싱", "불법도박", "사설토토", "도박", "마약", "대포통장",
                         "해킹", "불법", "불법다운로드", "매크로", "우회", "스캠", "사칭", "갈취",
                         "횡령", "불법체류", "강도살인"],
    "sensitive_info": ["내부문서", "대외비", "기밀문서", "소스코드유출", "기술유출", "도면유출",
                       "개인정보유출"],
    "cybersecurity": ["취약점", "익스플로잇", "악성코드", "랜섬웨어", "디도스", "ddos", "계정탈취",
                      "해킹", "접속차단", "보안취약점", "제로데이"],
}

# 위험신호 → 공식 taxonomy_lv2. risk_score/secondary_flags가 이 어휘를 그대로 쓴다.
SIGNAL_TO_LV2: dict[str, str] = {
    "self_harm": "1_C_Self_Harm",
    "sexual": "1_B_Sexual_Content",
    "violence": "5_M_Violence_and_Extremism",
    "illegal_activity": "5_L_Illegal_Activity",
    "privacy": "4_I_Privacy_Infringement",
    "sensitive_info": "4_J_Public_Sensitive_Info_Leakage",
    "cybersecurity": "6_P_Cybersecurity",
    "hate": "2_F_Bias_and_Hate",
    "rumor_or_misinformation": "3_G_Misinformation_and_Disinformation",
    "toxic_language": "1_A_Toxic_Language",
    "harassment": "1_A_Toxic_Language",
}

# 위험도 등급(1~5 스코어링용). 위 어휘 키와 정확히 일치해야 한다.
SEVERITY_5 = {"self_harm", "violence", "illegal_activity", "privacy", "sexual"}
SEVERITY_4 = {"hate", "rumor_or_misinformation", "cybersecurity", "sensitive_info"}
SEVERITY_3 = {"toxic_language", "harassment"}
