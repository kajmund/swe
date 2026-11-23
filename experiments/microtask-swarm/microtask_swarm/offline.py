"""Offline heuristic provider used by the executable pilot.

This provider makes no network or model call. It is a deterministic simulator
with four lexical personas, used only to exercise dispatching, verification,
early cancellation, scoring, and cost plumbing. Its quality numbers are not
model results and the report labels them ``offline_heuristic``.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from microtask_swarm.contracts import OPERATION_CHOICES, Kind
from microtask_swarm.executor import ChatRequest, ProviderResponse

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE = re.compile(r"[^.!?]+[.!?]?")
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does",
        "for", "from", "had", "has", "have", "how", "in", "is", "it", "its",
        "of", "on", "or", "that", "the", "their", "this", "to", "was", "were",
        "what", "when", "where", "which", "who", "why", "with",
    }
)
_NEGATIONS = frozenset({"no", "not", "never", "none", "without", "cannot"})


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in _WORD.findall(text.lower())
        if word not in _STOPWORDS
    }


def _overlap(query: str, text: str) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0
    return len(query_tokens & _tokens(text)) / len(query_tokens)


def _sentences(passage: str) -> list[str]:
    return [
        match.group(0).strip()
        for match in _SENTENCE.finditer(passage)
        if match.group(0).strip()
    ]


@dataclass(frozen=True, slots=True)
class Persona:
    name: str
    threshold: float
    latency_factor: float
    quote_always: bool = False


PERSONAS: dict[str, Persona] = {
    "strict": Persona("strict", 0.60, 1.35),
    "balanced": Persona("balanced", 0.40, 1.00),
    "lenient": Persona("lenient", 0.20, 0.72, quote_always=True),
    "conservative": Persona("conservative", 0.80, 1.55),
}

DEFAULT_PERSONA_BY_MODEL: dict[str, str] = {
    "gpt-oss-20b": "strict",
    "nemotron-3-super": "balanced",
    "mimo-v2.6-flash": "lenient",
    "deepseek-v4.1-flash": "conservative",
}


class OfflineHeuristicProvider:
    def __init__(
        self,
        *,
        model_ids: Sequence[str],
        simulated_latency_ms: float = 1.0,
    ) -> None:
        if simulated_latency_ms < 0:
            raise ValueError("simulated_latency_ms must be non-negative")
        self.model_ids = tuple(model_ids)
        self.simulated_latency_ms = simulated_latency_ms
        self._persona = {
            model: PERSONAS[DEFAULT_PERSONA_BY_MODEL.get(model, "balanced")]
            for model in self.model_ids
        }

    async def chat(self, request: ChatRequest) -> ProviderResponse:
        persona = self._persona[request.model_id]
        await asyncio.sleep(
            self.simulated_latency_ms * persona.latency_factor / 1000.0
        )
        output = self._answer(request, persona)
        system, user = request.system, request.user
        input_chars = len(system) + len(user)
        output_chars = len(json.dumps(output, sort_keys=True))
        input_tokens = max(1, input_chars // 4)
        output_tokens = max(1, output_chars // 4)
        return ProviderResponse(
            content=json.dumps(output, sort_keys=True),
            tokens={
                "input": input_tokens,
                "output": output_tokens,
                "reasoning": 0,
                "cache_read": input_tokens // 2,
                "cache_write": 0,
                "source": "estimated_chars_div_4",
            },
            billed_usd=None,
            price_period="peak",
        )

    def _answer(self, request: ChatRequest, persona: Persona) -> dict[str, Any]:
        if request.kind is Kind.COMBINED:
            data = request.input_data
            return {
                "relevance": self._relevance(
                    str(data["relevance"]["query"]), request.passage, persona
                ),
                "entailment": self._entailment(
                    str(data["entailment"]["hypothesis"]), request.passage, persona
                ),
                "atomicity": self._atomicity(str(data["atomicity"]["claim"])),
                "extraction": self._extraction(
                    request.passage, str(data["question"]), persona
                ),
                "operation": self._operation(str(data["operation"]["context"])),
            }
        if request.kind is Kind.RELEVANCE:
            return self._relevance(
                str(request.input_data["query"]), request.passage, persona
            )
        if request.kind is Kind.ENTAILMENT:
            return self._entailment(
                str(request.input_data["hypothesis"]), request.passage, persona
            )
        if request.kind is Kind.ATOMICITY:
            return self._atomicity(str(request.input_data["claim"]))
        if request.kind is Kind.EXTRACTION:
            return self._extraction(
                request.passage, str(request.input_data["question"]), persona
            )
        if request.kind is Kind.OPERATION:
            return self._operation(str(request.input_data["context"]))
        raise ValueError(f"offline provider cannot answer {request.kind!r}")

    def _relevance(
        self, query: str, passage: str, persona: Persona
    ) -> dict[str, bool]:
        return {"relevant": _overlap(query, passage) >= persona.threshold}

    def _entailment(
        self, hypothesis: str, passage: str, persona: Persona
    ) -> dict[str, str]:
        hypothesis_tokens = _tokens(hypothesis)
        passage_tokens = _tokens(passage)
        if (hypothesis_tokens & _NEGATIONS) and not (passage_tokens & _NEGATIONS):
            return {"label": "contradiction"}
        if _overlap(hypothesis, passage) >= persona.threshold:
            return {"label": "entailment"}
        return {"label": "neutral"}

    def _atomicity(self, claim: str) -> dict[str, bool]:
        lowered = claim.lower()
        connectors = lowered.count(" and ") + lowered.count(" or ") + lowered.count(";")
        return {"atomic": connectors == 0}

    def _extraction(
        self, passage: str, question: str, persona: Persona
    ) -> dict[str, list[str]]:
        sentences = _sentences(passage)
        if not sentences:
            return {"quotes": []}
        scored = sorted(
            ((_overlap(question, sentence), index, sentence) for index, sentence in enumerate(sentences)),
            reverse=True,
        )
        best_score, _, best = scored[0]
        if best_score >= persona.threshold or persona.quote_always:
            return {"quotes": [best]}
        return {"quotes": []}

    def _operation(self, context: str) -> dict[str, str]:
        lowered = context.lower()
        if "stated hypothesis" in lowered or "hypothesis" in lowered:
            operation = "classify_entailment"
        elif "single verifiable" in lowered or "atomic" in lowered:
            operation = "check_atomicity"
        elif "answers a specific question" in lowered or "relevant" in lowered:
            operation = "check_relevance"
        elif "answer the user directly" in lowered or "direct answer" in lowered:
            operation = "answer_question"
        elif "exact sentence" in lowered or "directly answers" in lowered or "directly states" in lowered:
            operation = "extract_quote"
        else:
            operation = "extract_quote"
        if operation not in OPERATION_CHOICES:
            operation = "extract_quote"
        return {"operation": operation}


__all__ = [
    "DEFAULT_PERSONA_BY_MODEL",
    "OfflineHeuristicProvider",
    "PERSONAS",
    "Persona",
]
