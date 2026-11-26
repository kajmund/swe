import pytest

pytest.importorskip("pydantic_ai.models.ollama")

import pydantic_ai
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider


def test_pydantic_ai_2_54_misses_colon_cloud_on_localhost() -> None:
    assert pydantic_ai.__version__ == "2.54.0"
    local = OllamaProvider(base_url="http://127.0.0.1:11434/v1")
    colon = OllamaModel("deepseek-v4.1-flash:cloud", provider=local)
    hyphen = OllamaModel("deepseek-v4.1-flash-cloud", provider=local)
    hosted = OllamaModel("deepseek-v4.1-flash:cloud", provider=OllamaProvider(base_url="https://ollama.com/v1"))
    assert colon.profile["supports_json_schema_output"] is True
    assert colon.profile["openai_supports_strict_tool_definition"] is False
    assert hyphen.profile["supports_json_schema_output"] is False
    assert hosted.profile["supports_json_schema_output"] is False
