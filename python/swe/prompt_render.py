"""Render validated prompt templates shared by the prompt stores."""

def render_prompt(prompts: dict[str, str], key: str, **kwargs: object) -> str:
    text = prompt_template(prompts, key)
    try:
        return text.format(**kwargs)
    except KeyError as exc:
        raise RuntimeError(f"Prompt '{key}' missing placeholder {exc}") from exc


def prompt_template(prompts: dict[str, str], key: str) -> str:
    """Return the prompt text with runtime placeholders still intact."""
    text = prompts.get(key)
    if text is None or not str(text).strip():
        raise RuntimeError(f"Active configuration is missing prompt '{key}'")
    return str(text)
