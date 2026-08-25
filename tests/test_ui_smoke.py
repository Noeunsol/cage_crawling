"""Phase 4·10 완료 조건 검증: 네 화면이 에러 없이 뜨고, 기본 흐름이 동작한다.

실제 브라우저 대신 streamlit.testing.v1.AppTest로 페이지 스크립트를 직접 실행해서 확인한다.
OpenAI/Tavily/SerpAPI를 실제로 호출하지 않으므로(실행 버튼을 누르지 않음) API key 없이도 돌아간다.
"""

from datetime import date

from streamlit.testing.v1 import AppTest


def test_all_pages_load_without_exception(monkeypatch):
    monkeypatch.setattr("ui.common.get_serpapi_usage", lambda key: None)  # 실제 네트워크 호출 방지
    for page in [
        "ui/pages/collection_setup.py",
        "ui/pages/domain_setup.py",
        "ui/pages/query_review.py",
        "ui/pages/run_collection.py",
        "ui/pages/data_browser.py",
    ]:
        at = AppTest.from_file(page)
        at.run(timeout=30)
        assert not at.exception, f"{page} raised: {at.exception}"


def test_data_browser_shows_lv2_chart_and_filterable_table(tmp_path, monkeypatch):
    from src.storage import database
    from src.storage.repositories import contents as contents_repo
    from src.storage.repositories import taxonomy_mappings as mappings_repo

    conn = database.connect(tmp_path / "test.db")
    monkeypatch.setattr("ui.common.get_db", lambda: conn)

    for i, (lv2, t, status) in enumerate([
        ("1_C_Self_Harm", "suicide", "accepted"),
        ("1_C_Self_Harm", "suicide", "accepted"),
        ("1_C_Self_Harm", "self_injury", "excluded"),
        ("1_A_Toxic_Language", "defamation", "accepted"),
    ]):
        content_id, _ = contents_repo.upsert_content(
            conn, title=f"글{i}", content=f"본문 내용 {i}입니다.", published_date="2026-01-01",
            canonical_url=f"https://a.com/{i}", source_name=None, source_domain="a.com",
            source_category=None, status=status, content_hash=f"h{i}",
        )
        mappings_repo.add_mapping(
            conn, content_id=content_id, taxonomy_lv2=lv2, type_name=t, decision=status,
            decision_reason=f"{status} test", prompt_name=None, prompt_version=None, model=None,
        )

    at = AppTest.from_file("ui/pages/data_browser.py")
    at.run(timeout=30)

    assert not at.exception
    assert any(m.label == "전체 accepted 수" and m.value == "3" for m in at.metric)
    assert any("4건" in c.value for c in at.caption)

    # LV2 필터를 1_C_Self_Harm으로 좁히면 3건(accepted 2 + excluded 1)만 남아야 한다
    lv2_select = at.selectbox[1]  # [0]=LV1, [1]=LV2, [2]=type
    lv2_select.select(next(o for o in lv2_select.options if "1_C_Self_Harm" in o))
    at.run(timeout=30)

    assert not at.exception
    assert any("3건" in c.value for c in at.caption)


def test_api_key_panel_lets_user_enter_missing_key_manually(monkeypatch):
    for env_name in ("OPENAI_API_KEY", "TAVILY_API_KEY", "SERPAPI_KEY"):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr("src.config.loader.load_dotenv", lambda *a, **kw: None)  # .env 자동 복구 방지

    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    assert not at.exception
    assert any("❌ **openai**" in w.value for w in at.markdown)

    at.text_input(key="manual_key_openai").set_value("sk-test-key")
    at.run(timeout=30)
    at.button(key="apply_key_openai").click()
    at.run(timeout=30)

    assert not at.exception
    assert any("✅ **openai**" in w.value for w in at.markdown)


def test_query_review_unchecks_types_that_already_have_both_providers_covered(tmp_path, monkeypatch):
    from src.storage import database
    from src.storage.repositories import queries as queries_repo

    # 실제 프로젝트 DB(database/content.db)를 절대 건드리지 않도록 임시 DB로 바꿔치기한다.
    conn = database.connect(tmp_path / "test.db")
    monkeypatch.setattr("ui.common.get_db", lambda: conn)

    for provider in ("tavily", "serpapi"):
        queries_repo.create_query(
            conn, taxonomy_lv2="1_A_Toxic_Language", type_name="cyberbullying_and_harassment",
            provider=provider, query_text=f"이미 있는 {provider} 검색어", status="generated", created_by="user",
        )

    at = AppTest.from_file("ui/pages/query_review.py")
    at.run(timeout=30)
    assert not at.exception

    covered_checkbox = next(c for c in at.checkbox if "cyberbullying_and_harassment" in c.label)
    assert covered_checkbox.value is False   # 이미 둘 다 있으니 기본적으로 재사용(체크 해제)

    uncovered_checkbox = next(c for c in at.checkbox if "csam_distribution" in c.label)
    assert uncovered_checkbox.value is True  # 검색어가 없는 type은 기본 체크


def test_run_collection_blocks_until_setup_confirmed():
    at = AppTest.from_file("ui/pages/run_collection.py")
    at.run(timeout=30)
    assert any("먼저 '1. 수집 설정'" in w.value for w in at.warning)
    assert len(at.button) == 0  # 실행 버튼 자체가 아직 없어야 한다


def test_run_collection_shows_preflight_once_setup_confirmed():
    at = AppTest.from_file("ui/pages/run_collection.py")
    at.session_state["setup"] = {
        "selected_lv2": ["1_C_Self_Harm"],
        "type_enabled": {},
        "target_count": 5,
        "candidate_multiplier": 1.5,
        "date_from": date(2025, 1, 1),
        "date_to": date(2026, 1, 1),
        "provider_ratio": {"tavily": 100, "serpapi": 0},
        "date_overrides": {},
        "provider_ratio_overrides": {},
        "confirmed": True,
    }
    at.run(timeout=30)

    assert not at.exception
    # 실제 DB를 그대로 쓰므로(get_db 미모킹) 과거 실행 기록이 몇 개든 있을 수 있다 —
    # 사전 검사 표(LV2 컬럼)가 정확히 하나 있는지만 확인한다.
    assert sum(1 for df in at.dataframe if "LV2" in df.value.columns) == 1
    assert any("검색어가 없어" in w.value for w in at.warning)   # 이 DB엔 아직 검색어가 없다
    run_button = next(b for b in at.button if "실행 시작" in b.label)
    assert run_button.disabled is True   # 비용 동의 체크 전이라 비활성 상태여야 한다


def test_run_collection_shows_results_table_with_content_detail(tmp_path, monkeypatch):
    from src.storage import database
    from src.storage.repositories import contents as contents_repo
    from src.storage.repositories import discoveries as discoveries_repo
    from src.storage.repositories import queries as queries_repo
    from src.storage.repositories import runs as runs_repo
    from src.storage.repositories import taxonomy_mappings as mappings_repo

    conn = database.connect(tmp_path / "test.db")
    monkeypatch.setattr("ui.common.get_db", lambda: conn)

    runs_repo.create_run(conn, "run-x", {})
    query_id = queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="q", status="used", created_by="user",
    )
    content_id, _ = contents_repo.upsert_content(
        conn, title="테스트 제목", content="테스트 본문 내용입니다.", published_date="2026-01-01",
        canonical_url="https://example.com/a", source_name=None, source_domain="example.com",
        source_category=None, status="accepted", content_hash="h1",
    )
    mappings_repo.add_mapping(
        conn, content_id=content_id, taxonomy_lv2="1_C_Self_Harm", type_name="suicide",
        decision="accepted", decision_reason="accepted | 한국 관련성: 한국 커뮤니티 글입니다",
        prompt_name=None, prompt_version=None, model=None,
    )
    discoveries_repo.record_discovery(
        conn, content_id=content_id, run_id="run-x", query_id=query_id,
        provider="tavily", returned_url="https://example.com/a", rank=1,
    )

    at = AppTest.from_file("ui/pages/run_collection.py")
    at.session_state["setup"] = {
        "selected_lv2": ["1_C_Self_Harm"], "type_enabled": {}, "target_count": 5, "candidate_multiplier": 1.5,
        "date_from": date(2025, 1, 1), "date_to": date(2026, 1, 1),
        "provider_ratio": {"tavily": 100, "serpapi": 0},
        "date_overrides": {}, "provider_ratio_overrides": {}, "confirmed": True,
    }
    at.session_state["last_run_id"] = "run-x"
    at.run(timeout=30)

    assert not at.exception
    results_table = next(df for df in at.dataframe if "기사 제목" in df.value.columns)
    assert results_table.value.iloc[0]["기사 제목"] == "테스트 제목"
    assert results_table.value.iloc[0]["텍소노미"] == "1_C_Self_Harm"
    assert results_table.value.iloc[0]["type"] == "suicide"
    assert results_table.value.iloc[0]["상태"] == "accepted"
    assert results_table.value.iloc[0]["수집 출처 사이트"] == "example.com"
    assert results_table.value.iloc[0]["한국 적합성"] == "한국 커뮤니티 글입니다"
    # AppTest는 대화형 dataframe 행 선택(체크박스 클릭) 시뮬레이션을 지원하지 않아,
    # 선택 시 상세 내용이 펼쳐지는 부분은 이 자동 테스트 범위 밖이다 (수동으로 확인함).


def _select_first_lv2(at):
    """1_A_Toxic_Language(첫 번째 LV1의 첫 번째 LV2) 체크박스를 켠다."""
    lv2_checkbox = next(c for c in at.checkbox if "Toxic_Language" in c.label)
    lv2_checkbox.check()
    at.run(timeout=30)
    return at


def test_collection_setup_lv2_selection_reveals_types_and_confirms(monkeypatch):
    monkeypatch.setattr("ui.common.get_serpapi_usage", lambda key: None)  # 실제 네트워크 호출 방지
    at = AppTest.from_file("ui/pages/collection_setup.py")
    at.run(timeout=30)

    _select_first_lv2(at)
    assert not at.exception
    type_checkboxes = [c for c in at.checkbox if c.label in {
        "cyberbullying_and_harassment", "cyberstalking", "defamation",
        "profanity_and_insults", "threats_and_intimidation",
    }]
    assert len(type_checkboxes) == 5  # 1_A_Toxic_Language의 type 5개

    confirm_button = next(b for b in at.button if "설정 확정" in b.label)
    assert confirm_button.disabled is False
    confirm_button.click()
    at.run(timeout=30)

    assert not at.exception
    assert any("설정을 확정했습니다" in s.value for s in at.success)


def test_collection_setup_lv2_date_override_adds_widgets(monkeypatch):
    monkeypatch.setattr("ui.common.get_serpapi_usage", lambda key: None)  # 실제 네트워크 호출 방지
    at = AppTest.from_file("ui/pages/collection_setup.py")
    at.run(timeout=30)
    _select_first_lv2(at)

    date_override_checkbox = next(c for c in at.checkbox if "기간 직접 조정" in c.label)
    date_override_checkbox.check()
    at.run(timeout=30)

    assert not at.exception
    assert len(at.date_input) == 2  # 전역 컨트롤은 없앴고, LV2 override 2개만 남는다


def test_query_review_lets_user_reactivate_used_query(tmp_path, monkeypatch):
    from src.storage import database
    from src.storage.repositories import queries as queries_repo
    from src.storage.repositories import query_executions as exec_repo
    from src.storage.repositories import runs as runs_repo

    conn = database.connect(tmp_path / "test.db")
    monkeypatch.setattr("ui.common.get_db", lambda: conn)

    runs_repo.create_run(conn, "run-1", {})
    query_id = queries_repo.create_query(
        conn, taxonomy_lv2="1_A_Toxic_Language", type_name="cyberbullying_and_harassment",
        provider="tavily", query_text="죽다 살아난 검색어", status="generated", created_by="user",
    )
    exec_id, _ = exec_repo.start_execution(
        conn, run_id="run-1", query_id=query_id, request_params={}, request_fingerprint="fp-1",
    )
    exec_repo.finish_execution(conn, exec_id, status="success", result_count=3)
    queries_repo.update_status(conn, query_id, "used")

    at = AppTest.from_file("ui/pages/query_review.py")
    at.run(timeout=30)
    assert not at.exception
    assert any("이미 사용됨 (1개)" in e.label for e in at.expander)

    next(b for b in at.button if "되돌리기" in b.label).click()
    at.run(timeout=30)

    assert not at.exception
    assert queries_repo.get_query(conn, query_id)["status"] == "generated"
    assert exec_repo.get_by_fingerprint(conn, "fp-1") is None
