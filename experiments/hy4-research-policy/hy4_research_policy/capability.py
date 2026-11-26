"""Which structured-output channels this experiment is allowed to send.

Ollama Cloud accepts a JSON schema and does not grammar-constrain it. Pydantic
AI 2.54 disables native schema output for ``-cloud`` names and for
``ollama.com``, and leaves ``:cloud`` on a local daemon marked as supported.
The tags in this experiment use the colon form.
"""

from __future__ import annotations

NATIVE_CHAT = "http://127.0.0.1:11434/api/chat"
OPENAI_BASE_URL = "http://127.0.0.1:11434/v1"


def _host(base_url: str) -> str:
    rest = base_url.split("://", 1)[-1]
    return rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower()


def routes_to_cloud(model_name: str, base_url: str) -> dict[str, object]:
    host = _host(base_url)
    signals: list[str] = []
    if host == "ollama.com" or host.endswith(".ollama.com"):
        signals.append("ollama-host")
    if model_name.endswith("-cloud"):
        signals.append("hyphen-cloud")
    if ":cloud" in model_name:
        signals.append("colon-cloud")
    return {"cloud": bool(signals), "signals": signals}


def output_modes(model_name: str, base_url: str) -> dict[str, str]:
    """Modes the experiment may send. Native schema stays refused on cloud."""

    cloud = routes_to_cloud(model_name, base_url)
    native = "refused_before_http" if cloud["cloud"] else "local_grammar"
    return {
        "native_structured": native,
        "tool": "supported",
        "prompt_json": "supported",
    }
