"""Multi-layer guardrail execution engine (CLAUDE_Advanced_Config.md
Section 3.5 / 37.7).

Input flow:  BERT (Layer 1, local) -> [escalate ->] Bedrock Guardrail (Layer 2)
Output flow: skips BERT entirely, goes direct to Bedrock Guardrail.

R39-style posture: unlike Redis rate limiting (the one thing this codebase
explicitly allows to fail open), a guardrail is a security control — a
Bedrock ApplyGuardrail failure fails CLOSED (blocks) rather than silently
passing content through. BERT failures (a local, non-network dependency)
are not expected to fail the same way; if the classifier itself raises,
that's a bug, not a transient outage, so it's allowed to propagate.

R30/R14: GuardrailLayerResult never carries prompt/response text — only
layer name, action, confidence, and a short category reason. Callers are
responsible for writing the audit event (this module has no AuditWriter
dependency, matching every other pure-decision module in this codebase —
see app.modules.security.policy_gate).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.modules.guardrails.bert_classifier import ONNXBertClassifier, ToxicityClassifier
from app.modules.guardrails.models import GuardrailDecision, GuardrailLayerResult, GuardrailPolicy
from app.shared.logging import get_logger

log = get_logger()

ClassifierFactory = Callable[[str], ToxicityClassifier]


class GuardrailEngine:
    def __init__(
        self,
        bedrock_client: Any,
        classifier_factory: ClassifierFactory = ONNXBertClassifier,
    ) -> None:
        self._bedrock = bedrock_client
        self._classifier_factory = classifier_factory
        self._classifiers: dict[str, ToxicityClassifier] = {}

    def _classifier_for(self, model_name: str) -> ToxicityClassifier:
        if model_name not in self._classifiers:
            self._classifiers[model_name] = self._classifier_factory(model_name)
        return self._classifiers[model_name]

    async def check_input(self, text: str, policy: GuardrailPolicy) -> GuardrailDecision:
        if not policy.input_enabled:
            return GuardrailDecision(blocked=False, layers=[])

        layers: list[GuardrailLayerResult] = []

        if policy.bert_enabled:
            classifier = self._classifier_for(policy.bert_model)
            score = await asyncio.to_thread(classifier.score, text)

            if score > policy.bert_block_threshold:
                layers.append(
                    GuardrailLayerResult(
                        layer="bert", action="block", confidence=score, reason="toxicity"
                    )
                )
                return GuardrailDecision(blocked=True, layers=layers)

            if score < policy.bert_escalate_threshold:
                layers.append(
                    GuardrailLayerResult(
                        layer="bert", action="pass", confidence=score, reason="safe"
                    )
                )
                return GuardrailDecision(blocked=False, layers=layers)

            layers.append(
                GuardrailLayerResult(
                    layer="bert", action="escalate", confidence=score, reason="unsure"
                )
            )

        if policy.bedrock_guardrail_id:
            bedrock_result = await self._call_bedrock_guardrail(text, policy, source="INPUT")
            layers.append(bedrock_result)
            if bedrock_result.action == "block":
                return GuardrailDecision(blocked=True, layers=layers)
        elif layers and layers[-1].action == "escalate":
            # BERT was unsure but there's no Layer 2 configured to consult —
            # nothing to escalate to. Recorded as its own layer entry so the
            # audit trail shows this was a deliberate default, not a skipped
            # check.
            layers.append(
                GuardrailLayerResult(
                    layer="bedrock",
                    action="pass",
                    reason="no_bedrock_guardrail_configured",
                )
            )

        return GuardrailDecision(blocked=False, layers=layers)

    async def check_output(self, text: str, policy: GuardrailPolicy) -> GuardrailDecision:
        if not policy.output_enabled:
            return GuardrailDecision(blocked=False, layers=[])

        if not policy.bedrock_guardrail_id:
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
