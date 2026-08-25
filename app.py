"""Streamlit 진입점. 페이지 네비게이션만 담당하고 비즈니스 로직은 두지 않는다 (2.4절)."""

import streamlit as st

from ui.common import get_configs, render_api_key_panel

st.set_page_config(page_title="AI Safety 콘텐츠 수집기", page_icon="🕸️", layout="wide")

render_api_key_panel(get_configs()["providers"])

pages = [
    st.Page("ui/pages/collection_setup.py", title="1. 수집 설정", icon="🎯"),
    st.Page("ui/pages/domain_setup.py", title="2. 도메인 설정", icon="🌐"),
    st.Page("ui/pages/query_review.py", title="3. 검색어 검토", icon="🔍"),
    st.Page("ui/pages/run_collection.py", title="4. 실행 및 결과", icon="🚀"),
    st.Page("ui/pages/data_browser.py", title="5. 데이터 탐색", icon="📊"),
]
st.navigation(pages).run()
