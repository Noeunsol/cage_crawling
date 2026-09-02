"""내보낸 CSV의 콘텐츠 품질을 OpenAI로 점검한다."""

from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from src.config.loader import PROJECT_ROOT
from src.quality_check import load_db_scores, score_csv, write_scores
from ui.common import get_configs

st.title("✅ 6. 퀄리티 체크")
st.caption("data 아래 CSV를 조회하고 기존 quality_score 기준으로 점검합니다. 실행 시 OpenAI 비용이 발생합니다.")

files = sorted(path for path in (PROJECT_ROOT / "data").rglob("*.csv") if not path.stem.endswith("_quality"))
if not files:
    st.info("data에 검사할 CSV가 없습니다.")
    st.stop()

labels = [str(path.relative_to(PROJECT_ROOT)) for path in files]
selected = st.selectbox("검사할 CSV", labels)
limit = st.number_input("검사 건수", min_value=1, max_value=100, value=3)
source_path = PROJECT_ROOT / selected
result_path = source_path.with_name(f"{source_path.stem}_quality.csv")

with st.expander("원본 데이터 보기", expanded=True):
    source_df = pd.read_csv(source_path)
    st.caption(f"총 {len(source_df)}건")
    st.dataframe(source_df, hide_index=True, width="stretch")

if st.button("퀄리티 체크 실행", type="primary"):
    progress = st.progress(0)
    try:
        with st.spinner("콘텐츠를 채점하고 있습니다..."):
            rows = score_csv(
                source_path, get_configs(), int(limit),
                lambda n, total: progress.progress(n / total),
            )
        write_scores(rows, result_path)
        st.session_state["quality_check_rows"] = rows
        st.session_state["quality_check_source"] = selected
    except Exception as error:
        st.error(f"퀄리티 체크에 실패했습니다: {error}")

test_db_path = PROJECT_ROOT / "database" / "test_content.db"
if st.session_state.get("quality_check_source") == selected:
    df = pd.DataFrame(st.session_state.get("quality_check_rows", []))
elif "test" in source_path.relative_to(PROJECT_ROOT / "data").parts and test_db_path.exists():
    db_rows = load_db_scores(source_path, test_db_path)
    df = pd.DataFrame(db_rows)
    if db_rows:
        st.info(f"실험 DB에 저장된 기존 결과 {len(db_rows)}/{len(source_df)}건을 표시합니다.")
elif result_path.exists():
    df = pd.read_csv(result_path)
    st.info(f"저장된 기존 결과 {len(df)}건을 표시합니다.")
else:
    df = pd.DataFrame()

if not df.empty:
    st.metric("평균 overall", f"{df['overall'].mean():.2f} / 5")
    st.dataframe(df, hide_index=True, width="stretch")
    output = io.StringIO()
    df.to_csv(output, index=False)
    st.download_button(
        "결과 CSV 다운로드", output.getvalue().encode("utf-8-sig"),
        file_name=f"{selected.rsplit('/', 1)[-1][:-4]}_quality.csv", mime="text/csv",
    )
