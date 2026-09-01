import json
from types import SimpleNamespace

from src.query.freshness import fetch_fresh_vocabulary


def test_fetch_fresh_vocabulary_requires_web_search():
    calls = []
    client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **kwargs: calls.append(kwargs)
            or SimpleNamespace(
                output_text=json.dumps({"terms": ["최근 표현"]}),
                usage=SimpleNamespace(input_tokens=12, output_tokens=3),
            )
        )
    )
    prompt_cfg = {
        "name": "fresh_vocabulary",
        "required_inputs": ["type_name", "definition"],
        "system_prompt": "{type_name}",
        "user_prompt": "{definition}",
        "output_schema": {"type": "object"},
    }

    result = fetch_fresh_vocabulary(
        client, prompt_cfg, type_name="news", definition="definition"
    )
    assert result.terms == ["최근 표현"]
    assert (result.prompt_tokens, result.completion_tokens, result.web_search_calls) == (12, 3, 1)
    assert calls[0]["tools"] == [{"type": "web_search"}]
    assert calls[0]["tool_choice"] == "required"
