"""Phase 3 완료 조건 검증: provider가 안 섞이고, 기간 표현/site: 연산자는 모델이 만들어도 걸러진다."""

import json
from types import SimpleNamespace

from src.query.generator import _filter_queries, generate_queries
from src.query.repository import list_used_queries, reactivate_query, save_generated_queries
from src.storage import database
from src.storage.repositories import queries as queries_repo
from src.storage.repositories import query_executions as exec_repo
from src.storage.repositories import runs as runs_repo
from src.utils.prompts import load_prompt


class FakeOpenAI:
    """client.chat.completions.create(...) 만 흉내내는 가짜 OpenAI 클라이언트."""

    def __init__(self, queries: list[str]):
        self._queries = queries
        self.last_call = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.last_call = kwargs
        content = json.dumps({"queries": self._queries}, ensure_ascii=False)
        message = SimpleNamespace(content=content)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_filter_queries_drops_date_expressions_and_site_operator_and_dupes():
    result = _filter_queries([
        "자살 예방 상담 후기",
        "2026년 자살 예방 상담 후기",     # 연도 표현 -> 제외
        "최근 1년 자살 관련 뉴스",         # 상대 기간 표현 -> 제외
        "site:kin.naver.com 자살 상담",   # site: 연산자 -> 제외 (discovery 단계가 담당)
        "자살 예방 상담 후기",             # 배치 내 중복 -> 제외
        "",                               # 빈 문자열 -> 무시
    ])
    assert result.accepted == ["자살 예방 상담 후기"]
    reasons = {reason for _, reason in result.rejected}
    assert reasons == {"date_expression", "site_operator", "duplicate_within_batch"}


def test_generate_queries_renders_provider_specific_prompt_and_filters_result():
    prompt_cfg = load_prompt("query_generation")
    fake_client = FakeOpenAI(queries=["자살 생각 커뮤니티 글", "2026년 자살 통계"])

    result = generate_queries(
        fake_client,
        prompt_cfg,
        taxonomy_lv2="1_C_Self_Harm",
        type_name="suicide",
        definition="자살 방법, 치명성 향상, 계획 수립 등을 안내하거나 자살을 정당화·미화·권유하는 행위",
        include_criteria=[],
        exclude_criteria=[],
        provider="tavily",
        query_count=2,
        model="gpt-4o-mini",
    )

    assert result.accepted == ["자살 생각 커뮤니티 글"]
    assert result.rejected == [("2026년 자살 통계", "date_expression")]

    user_message = fake_client.last_call["messages"][1]["content"]
    assert "provider: tavily" in user_message
    assert "정확히 2개" in user_message


def test_tavily_and_serpapi_queries_are_saved_separately(tmp_path):
    conn = database.connect(tmp_path / "test.db")

    save_generated_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_texts=["자살 생각 커뮤니티 글"], prompt_version="1", model="gpt-4o-mini",
    )
    save_generated_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi",
        query_texts=["자살 생각 커뮤니티 글"], prompt_version="1", model="gpt-4o-mini",
    )

    tavily_rows = queries_repo.list_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily"
    )
    serpapi_rows = queries_repo.list_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="serpapi"
    )

    # 같은 텍스트라도 provider가 다르면 서로 다른 행으로 저장된다 (5.4절: provider별 완전 분리).
    assert len(tavily_rows) == 1
    assert len(serpapi_rows) == 1
    assert tavily_rows[0]["id"] != serpapi_rows[0]["id"]


def test_saving_same_query_twice_is_idempotent(tmp_path):
    conn = database.connect(tmp_path / "test.db")

    ids1 = save_generated_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_texts=["자살 생각 커뮤니티 글"], prompt_version="1", model="gpt-4o-mini",
    )
    ids2 = save_generated_queries(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_texts=["자살 생각 커뮤니티 글"], prompt_version="1", model="gpt-4o-mini",
    )

    assert ids1 == ids2
    assert len(queries_repo.list_queries(conn, provider="tavily")) == 1


def test_reactivate_query_restores_status_and_frees_fingerprint(tmp_path):
    conn = database.connect(tmp_path / "test.db")
    runs_repo.create_run(conn, "run-1", {})

    query_id = queries_repo.create_query(
        conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily",
        query_text="자살 생각 커뮤니티 글", status="generated", created_by="user",
    )
    # 검색은 성공해서 used + fingerprint까지 남았는데, 그 뒤가 죽어서 결과는 저장 안 된 상황을 흉내낸다.
    exec_id, _ = exec_repo.start_execution(
        conn, run_id="run-1", query_id=query_id, request_params={}, request_fingerprint="fp-1",
    )
    exec_repo.finish_execution(conn, exec_id, status="success", result_count=5)
    queries_repo.update_status(conn, query_id, "used")

    assert list_used_queries(conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily")
    assert exec_repo.get_by_fingerprint(conn, "fp-1") is not None

    reactivate_query(conn, query_id)

    assert queries_repo.get_query(conn, query_id)["status"] == "generated"
    assert not list_used_queries(conn, taxonomy_lv2="1_C_Self_Harm", type_name="suicide", provider="tavily")
    assert exec_repo.get_by_fingerprint(conn, "fp-1") is None  # 재검색이 실제로 다시 일어날 수 있다
