"""Live dispatch is unavailable in this package."""

from __future__ import annotations

from typing import Any


class LiveDispatchRefused(RuntimeError):
    """A paid model or evaluator call was requested and refused."""


def refuse_live_dispatch() -> None:
    raise LiveDispatchRefused(
        "Paid Hy4, Hy4 Flash, DeepSeek Flash, and JEV calls are refused. "
        "Report the estimate and wait for explicit approval before any dispatch."
    )


def credential_status(protocol: dict[str, Any], environ: dict[str, str]) -> dict[str, str]:
    """Report whether named credentials are non-empty. Values are not returned."""

    names = {
        str(arm["credential_env"])
        for arm in protocol["arms"]
        if arm.get("credential_env")
    }
    names.add(str(protocol["evaluator"]["credential_env"]))
    return {
        name: "present" if environ.get(name, "").strip() else "absent" for name in sorted(names)
    }
