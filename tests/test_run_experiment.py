from types import SimpleNamespace

from experiments import run_experiment


def test_ensure_queries_only_generates_the_missing_count(monkeypatch):
    existing = {"tavily": [object()], "serpapi": [object(), object(), object()]}
    requested = []
    configs = {
        "providers": {"openai": {"query_generation_model": "query-model"}, "query_limits": {}},
        "taxonomy": {"taxonomy": [{
            "lv2_id": "LV2", "types": [{
                "name": "type_a", "definition": "definition", "include_criteria": [], "exclude_criteria": [],
            }],
        }]},
    }

    monkeypatch.setattr(run_experiment, "suggested_query_counts", lambda *args, **kwargs: (3, 3))
    monkeypatch.setattr(
        run_experiment.repository, "list_active_queries",
        lambda _conn, *, taxonomy_lv2, type_name, provider: existing[provider],
    )
    monkeypatch.setattr(run_experiment.generator, "build_client", lambda _providers: object())
    monkeypatch.setattr(run_experiment.generator, "build_async_client", lambda _providers: object())
    monkeypatch.setattr(run_experiment, "load_prompt", lambda name: {"version": 1})
    monkeypatch.setattr(
        run_experiment.vocabulary, "resolve_vocabulary",
        lambda **kwargs: ([], "cached", None),
    )
    monkeypatch.setattr(run_experiment.vocabulary, "effective_exclude_criteria", lambda _cfg: [])

    async def generate_queries_async(*args, query_count, **kwargs):
        requested.append(query_count)
        return SimpleNamespace(accepted=["q1", "q2"], prompt_tokens=1, completion_tokens=1, elapsed_s=0.1)

    monkeypatch.setattr(run_experiment.generator, "generate_queries_async", generate_queries_async)
    def save_queries(*args, **kwargs):
        existing[kwargs["provider"]].extend(kwargs["query_texts"])

    monkeypatch.setattr(run_experiment.repository, "save_generated_queries", save_queries)
    monkeypatch.setattr(run_experiment.generation_calls_repo, "record_call", lambda *args, **kwargs: None)

    run_experiment.ensure_queries(object(), configs, [("LV2", "type_a")], {"target_count": 3})

    assert requested == [2]
