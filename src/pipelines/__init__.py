"""실행 파이프라인(모드별) 서브패키지.

pipeline.py(facade)가 재export하는 실제 구현이 여기 모인다:
- keyword.py / trend.py / gap_filling.py : 3개 실행 모드
- stages.py / persist.py / taxonomy_adjudication.py / _trend_util.py : 모드 공용/보조 헬퍼
"""
