"""Multi-layer guardrail execution engine (CLAUDE.md Section 3.5 / 37.7 /
37.15).

Input flow:  BERT (Layer 1, local, 4 sub-checks) -> [toxicity escalate ->]
             Bedrock Guardrail (Layer 2)
Output flow: skips BERT entirely, goes direct to Bedrock Guardrail.

Layer 1 sub-checks (Section 37.15, closed 2026-08-16): toxicity, NSFW,
prompt injection, gibberish — each backed by its own ONNXBertClassifier
instance (own model directory, own target label keyword). Only toxicity
has an `escalate_threshold` in the schema and therefore only toxicity ever
escalates to Bedrock; the other three are single-threshold local
block/pass checks — a block from ANY of the four short-circuits
immediately (no AWS call, no tokens spent), matching the original
toxicity-only design's efficiency goal.

R39-style posture: unlike Redis rate limiting (the one thing this codebase
explicitly allows to fail open), a guardrail is a security control — a
Bedrock ApplyGuardrail failure fails CLOSED (blocks) rather than silently
passing content through. BERT failures (a local, non-network dependency)
are not expected to fail the same way; if a classifier itself raises,
that's a bug, not a transient outage, so it's allowed to propagate.

R30/R14: GuardrailLayerResult never carries prompt/response text — only
layer name, action, confidence, and a short category reason. Callers are
responsible for writing the audit event (this module has no AuditWriter
dependency, matching every other pure-decision module in this codebase —
see app.modules.security.policy_gate).

Still schema-only (see models.py): `TopicConfig.allowed_topics` and
`ComplianceConfig`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from app.modules.guardrails.bert_classifier import ONNXBertClassifier, ToxicityClassifier
from app.modules.guardrails.models import (
    DEFAULT_GIBBERISH_MODEL,
    DEFAULT_NSFW_MODEL,
    DEFAULT_PROMPT_INJECTION_MODEL,
    DEFAULT_TOXICITY_MODEL,
    GuardrailDecision,
    GuardrailLayerResult,
    GuardrailPolicy,
)
from app.shared.logging import get_logger

log = get_logger()

ClassifierFactory = Callable[[str, str], ToxicityClassifier]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    return sentences or [text]


class GuardrailEngine:
    def __init__(
        self,
        bedrock_client: Any,
        classifier_factory: ClassifierFactory = ONNXBertClassifier,
        mock_enabled: bool = False,
    ) -> None:
        self._bedrock = bedrock_client
        self._classifier_factory = classifier_factory
        self._classifiers: dict[tuple[str, str], ToxicityClassifier] = {}
        # settings.mock_bedrock_guardrails — skips the real Bedrock
        # ApplyGuardrail runtime call entirely, for dev environments
        # without real Bedrock access (same flag/rationale as
        # BedrockGuardrailProvisioner's mock_enabled, which only covers
        # provisioning a guardrail — this covers actually running one).
        self._mock_enabled = mock_enabled

    def _classifier_for(self, model_name: str, target_keyword: str) -> ToxicityClassifier:
        key = (model_name, target_keyword)
        if key not in self._classifiers:
            self._classifiers[key] = self._classifier_factory(model_name, target_keyword)
        return self._classifiers[key]

    async def _score_with_validation(
        self, classifier: ToxicityClassifier, text: str, validation: str
    ) -> float:
        if validation == "full_text":
            return await asyncio.to_thread(classifier.score, text)
        scores = await asyncio.gather(
            *[asyncio.to_thread(classifier.score, sentence) for sentence in _split_sentences(text)]
        )
        return max(scores) if scores else 0.0

    async def _run_binary_check(
        self,
        text: str,
        *,
        model_name: str,
        target_keyword: str,
        threshold: float,
        validation: str,
        reason: str,
        layers: list[GuardrailLayerResult],
    ) -> GuardrailDecision | None:
        """Runs one single-threshold local check (NSFW/prompt-injection/
        gibberish — anything except toxicity, which has its own
        escalate-to-Bedrock path). Returns a blocked GuardrailDecision if
        this check fires, else None (caller continues to the next check)."""
        classifier = self._classifier_for(model_name, target_keyword)
        score = await self._score_with_validation(classifier, text, validation)
        if score > threshold:
            layers.append(
                GuardrailLayerResult(layer="bert", action="block", confidence=score, reason=reason)
            )
            return GuardrailDecision(blocked=True, layers=layers)
        layers.append(
            GuardrailLayerResult(layer="bert", action="pass", confidence=score, reason=reason)
        )
        return None

    def _bedrock_configured(self, policy: GuardrailPolicy) -> bool:
        return bool(policy.bedrock_enabled and policy.bedrock_guardrail_id)

    async def check_input(self, text: str, policy: GuardrailPolicy) -> GuardrailDecision:
        layers: list[GuardrailLayerResult] = []
        bert = policy.bert
        # True only when check_toxicity ran AND scored clearly safe — the
        # one condition (matching the pre-4-check design exactly) that
        # skips Bedrock entirely. Every other path — bert.enabled=False,
        # check_toxicity=False, or toxicity landing in its escalate band —
        # falls through to "maybe consult Bedrock" below. The other 3
        # checks never set this: they're pure local block/pass gates that
        # don't participate in the escalate-to-Bedrock decision.
        toxicity_resolved_safe = False

        if bert.enabled:
            if bert.check_toxicity:
                classifier = self._classifier_for(DEFAULT_TOXICITY_MODEL, "toxic")
                score = await asyncio.to_thread(classifier.score, text)

                if score > bert.block_threshold:
                    layers.append(
                        GuardrailLayerResult(
                            layer="bert", action="block", confidence=score, reason="toxicity"
                        )
                    )
                    return GuardrailDecision(blocked=True, layers=layers)

                if score < bert.escalate_threshold:
                    layers.append(
                        GuardrailLayerResult(
                            layer="bert", action="pass", confidence=score, reason="safe"
                        )
                    )
                    toxicity_resolved_safe = True
                else:
                    layers.append(
                        GuardrailLayerResult(
                            layer="bert", action="escalate", confidence=score, reason="unsure"
                        )
                    )

            if bert.check_nsfw:
                result = await self._run_binary_check(
                    text,
                    model_name=DEFAULT_NSFW_MODEL,
                    target_keyword="nsfw",
                    threshold=bert.nsfw_threshold,
                    validation=bert.nsfw_validation,
                    reason="nsfw",
                    layers=layers,
                )
                if result is not None:
                    return result

            if bert.check_prompt_injection:
                result = await self._run_binary_check(
                    text,
                    model_name=DEFAULT_PROMPT_INJECTION_MODEL,
                    target_keyword="injection",
                    threshold=bert.prompt_injection_threshold,
                    validation="full_text",  # no per-check validation field in the schema
                    reason="prompt_injection",
                    layers=layers,
                )
                if result is not None:
                    return result

            if bert.check_gibberish:
                result = await self._run_binary_check(
                    text,
                    model_name=DEFAULT_GIBBERISH_MODEL,
                    target_keyword="gibberish",
                    threshold=bert.gibberish_threshold,
                    validation=bert.gibberish_validation,
                    reason="gibberish",
                    layers=layers,
                )
                if result is not None:
                    return result

        if toxicity_resolved_safe:
            return GuardrailDecision(blocked=False, layers=layers)

        # Reached when: bert.enabled=False, check_toxicity=False, or
        # toxicity landed in its escalate band — mirrors the original
        # single-check design's fall-through exactly.
        if self._bedrock_configured(policy):
            bedrock_result = await self._call_bedrock_guardrail(text, policy, source="INPUT")
            layers.append(bedrock_result)
            if bedrock_result.action == "block":
                return GuardrailDecision(blocked=True, layers=layers)
        elif layers and layers[-1].action == "escalate":
            # Toxicity was unsure but there's no Layer 2 configured to
            # consult — nothing to escalate to. Recorded as its own layer
            # entry so the audit trail shows this was a deliberate
            # default, not a skipped check.
            layers.append(
                GuardrailLayerResult(
                    layer="bedrock",
                    action="pass",
                    reason="no_bedrock_guardrail_configured",
                )
            )

        return GuardrailDecision(blocked=False, layers=layers)

    async def check_output(self, text: str, policy: GuardrailPolicy) -> GuardrailDecision:
        if not self._bedrock_configured(policy):
            return GuardrailDecision(
                blocked=False,
                layers=[
                    GuardrailLayerResult(
                        layer="bedrock",
                        action="pass",
                        reason="no_bedrock_guardrail_configured",
                    )
                ],
            )

        layer_result, sanitised_text = await self._call_bedrock_guardrail_with_output(
            text, policy, source="OUTPUT"
        )
        if layer_result.action == "block":
            return GuardrailDecision(blocked=True, layers=[layer_result])
        return GuardrailDecision(
            blocked=False,
            layers=[layer_result],
            sanitised_text=sanitised_text if sanitised_text != text else None,
        )

    async def _call_bedrock_guardrail(
        self, text: str, policy: GuardrailPolicy, *, source: str
    ) -> GuardrailLayerResult:
        result, _ = await self._call_bedrock_guardrail_with_output(text, policy, source=source)
        return result

    async def _call_bedrock_guardrail_with_output(
        self, text: str, policy: GuardrailPolicy, *, source: str
    ) -> tuple[GuardrailLayerResult, str]:
        if self._mock_enabled:
            return (
                GuardrailLayerResult(layer="bedrock", action="pass", reason="mocked_pass"),
                text,
            )

        try:
            response = await asyncio.to_thread(
                self._bedrock.apply_guardrail,
                guardrailIdentifier=policy.bedrock_guardrail_id,
                guardrailVersion=policy.bedrock_guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
        except Exception:
            # Fail CLOSED (R39-style: a security control, not rate limiting)
            # — an AWS outage must never silently let unchecked content
            # through.
            log.warning("guardrails.bedrock.call_failed", source=source, exc_info=True)
            return (
                GuardrailLayerResult(
                    layer="bedrock", action="block", reason="bedrock_guardrail_unavailable"
                ),
                text,
            )

        intervened = response.get("action") == "GUARDRAIL_INTERVENED"
        outputs = response.get("outputs") or []
        sanitised_text = outputs[0]["text"] if outputs and "text" in outputs[0] else text
        return (
            GuardrailLayerResult(
                layer="bedrock",
                action="block" if intervened else "pass",
                reason="guardrail_intervened" if intervened else "passed",
            ),
            sanitised_text,
        )
