"""KB ingestion-time security scan (Sprint 4 Phase 8 — S-14, CLAUDE.md
Section 66/R69).

Two of R69's three layers, implemented for real here:
  1. Document type/size/structural validation.
  2. Prompt-injection content scan — reuses the SAME ONNX BERT classifier
     app/modules/guardrails/bert_classifier.py already loads for the
     Playground's own input guardrail (GuardrailEngine, Section 37.7/
     37.15/29) via a synthetic, prompt-injection-only GuardrailPolicy —
     not a second, different classifier or a hand-rolled keyword scan.

The third layer (malware/virus scan, e.g. GuardDuty Malware Protection
for S3) is DELIBERATELY NOT implemented here — flagged explicitly in
CLAUDE.md's S-14 status note, not silently skipped or faked with a
fake-pass result. Real reasons, not a shortcut:
  - GuardDuty Malware Protection is event-driven/async (S3 event ->
    scan -> tag/EventBridge finding, seconds to minutes later) — not a
    synchronous API this Runtime could call and block on inside an
    upload/sync request the way the two checks below are.
  - The KB documents bucket is CUSTOMER-configured
    (PlatformSettingsRecord.kb_s3_bucket) — it may not even be a bucket
    Panasa's own generated Terraform manages, so there's no obvious
    place to attach a GuardDuty IaC resource the way
    guardrails.tf.j2/human_loop.tf.j2 attach IAM to an agent's own
    execution role.
  - A synchronous, Panasa-run malware scan would need a real scanning
    engine dependency (e.g. a ClamAV daemon) that doesn't exist anywhere
    in this stack today.
  This is a genuine architectural decision (new customer-Terraform
  resource? new scanning-engine dependency? which deployment mode(s)?)
  that CLAUDE.md's own top-level directive says to surface, not decide
  unilaterally.

CLAUDE.md Section 66.4 also assumes a `document_reader` skill already
declares `supported_types`/`max_file_size_mb` to reuse — confirmed
against the real codebase that no such module exists (Section 29/38.3's
Skill catalog has no file-type/size concept at all). The only real,
pre-existing list is app/api/v1/knowledge_bases.py's own
`_ALLOWED_DOCUMENT_EXTENSIONS` — reused from there (re-exported here as
the new canonical home) rather than inventing a second list, which is
the closest match to R69's actual "reuse, don't invent a second list"
intent given what exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from app.modules.guardrails.engine import GuardrailEngine
from app.modules.guardrails.models import BertConfig, GuardrailPolicy

# Section 47/Section 66.4 — same list app/api/v1/knowledge_bases.py already
# enforces at upload time; this module is now the canonical home for it
# (knowledge_bases.py imports it from here).
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".csv"}

# No size limit existed anywhere before this phase (confirmed gap) — 50MB
# matches the figure CLAUDE.md Section 29 itself cites for document_reader's
# own (never-built) max_file_size_mb default.
MAX_DOCUMENT_SIZE_BYTES = 50 * 1024 * 1024

# Plain-text formats this module can actually extract text from without a
# new parsing dependency (pypdf/python-docx are not in this project's
# requirements). .pdf/.docx/.doc are validated (extension + size) but their
# CONTENT is not prompt-injection-scanned — see scan_for_prompt_injection's
# own docstring; a real gap, not a silent false "clean" claim.
_TEXT_EXTRACTABLE_EXTENSIONS = {".txt", ".md", ".html", ".csv"}

_PROMPT_INJECTION_THRESHOLD = BertConfig().prompt_injection_threshold


@dataclass
class ScanResult:
    passed: bool
    reason: str | None = None  # "validation" | "prompt_injection" | None when passed


def validate_document(filename: str, content: bytes) -> ScanResult:
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        return ScanResult(passed=False, reason="validation")
    if len(content) > MAX_DOCUMENT_SIZE_BYTES:
        return ScanResult(passed=False, reason="validation")
    if len(content) == 0:
        return ScanResult(passed=False, reason="validation")
    return ScanResult(passed=True)


def _extract_text(filename: str, content: bytes) -> str | None:
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in _TEXT_EXTRACTABLE_EXTENSIONS:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def _prompt_injection_only_policy(tenant_id: str) -> GuardrailPolicy:
    """A throwaway policy object, never persisted — check_input() only
    ever reads .bert/.bedrock_enabled/.bedrock_guardrail_id off it, so the
    identity fields below are placeholders, not a real saved
    GuardrailPolicyRecord. bedrock_enabled=False and every OTHER bert
    sub-check disabled means check_input() runs EXACTLY the prompt-
    injection classifier and nothing else."""
    return GuardrailPolicy(
        policy_id="kb-ingestion-scan",
        tenant_id=tenant_id,
        name="KB ingestion-time prompt-injection scan",
        description="Synthetic, not persisted — see ingestion_scan.py",
        created_at="1970-01-01T00:00:00Z",
        updated_at="1970-01-01T00:00:00Z",
        created_by="system",
        bert=BertConfig(
            check_toxicity=False,
            check_nsfw=False,
            check_prompt_injection=True,
            check_gibberish=False,
        ),
        bedrock_enabled=False,
    )


async def scan_for_prompt_injection(
    filename: str, content: bytes, tenant_id: str, guardrail_engine: GuardrailEngine
) -> ScanResult:
    """R69 layer 3 — reuses GuardrailEngine.check_input(), the SAME method
    (not a parallel reimplementation) the Playground's own input guardrail
    already calls, matching R69's "the SAME classifier the runtime
    guardrail chain uses" requirement exactly.

    Only runs for plain-text-extractable formats (see
    _TEXT_EXTRACTABLE_EXTENSIONS) — binary formats (.pdf/.docx/.doc) pass
    through this check unscanned, since no text-extraction dependency
    exists in this project yet (a real, disclosed gap, not a silent
    false-clean claim; validate_document's own extension/size checks
    still apply to every format equally)."""
    text = _extract_text(filename, content)
    if text is None or not text.strip():
        return ScanResult(passed=True)

    decision = await guardrail_engine.check_input(text, _prompt_injection_only_policy(tenant_id))
    if decision.blocked:
        return ScanResult(passed=False, reason="prompt_injection")
    return ScanResult(passed=True)
