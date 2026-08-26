from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Protocol, Sequence, cast

from local_agent_lab.memory.audit import tombstone_source
from local_agent_lab.memory.chatgpt_ingest import import_chatgpt_export
from local_agent_lab.memory.domain_scoping import classify_text_domains, dominant_domain
from local_agent_lab.memory.embeddings import (
    LOCAL_VECTOR_BACKEND,
    cosine_similarity,
    deterministic_fallback_embedding,
    embed_missing_chunks,
    fallback_model_spec,
    load_local_vector,
)
from local_agent_lab.memory.observability import memory_db_path
from local_agent_lab.memory.privacy import redact_obvious_secrets
from local_agent_lab.memory.search import search_chatgpt_memory
from local_agent_lab.memory.subjects import assign_conversation_subject
from local_agent_lab.security.paths import safe_write_text
from local_agent_lab.sqlite_utils import sqlite_connection


EVAL_SCHEMA_VERSION = "offline_synthetic_retrieval_report_v1.2"
DATASET_VERSION = "memory_eval_dataset_v1.2.0"
CONFIG_VERSION = "memory_eval_config_v1.2.0"
FAILURE_TAXONOMY_VERSION = "memory_eval_failures_v1.2"
TOP_K = 5
EMBEDDING_DIMENSION = 128
EVAL_REFERENCE_TIME = datetime(2026, 8, 23, 0, 0, 0, tzinfo=timezone.utc)
VARIANT_NAMES = ("no_memory", "raw_fts", "vector_only", "governed_hybrid")
OPENAI_SECRET_CANARY = "sk-" + "evalabcdefghijklmnopqrstuvwxyz123456"
GITHUB_SECRET_CANARY = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
PASSWORD_SECRET_CANARY = "-".join(("eval", "hunter2", "secret"))
JWT_SECRET_CANARY = ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJldmFsLXVzZXIifQ", "signatureabcdefgh"))

# These thresholds are part of the hashed configuration and are declared before
# any adapter executes. Sparse-gold precision@5 has a theoretical maximum of 0.2
# for cases with one known relevant source, so MRR and recall carry more weight.
GOVERNED_THRESHOLDS: dict[str, tuple[str, float]] = {
    "mrr": ("min", 0.65),
    "precision_at_5": ("min", 0.15),
    "recall_at_5": ("min", 0.70),
    "abstention_accuracy": ("min", 0.75),
    "provenance_accuracy": ("min", 1.0),
    "wrong_domain_rate": ("max", 0.0),
    "stale_selection_rate": ("max", 0.0),
    "sensitive_leakage_rate": ("max", 0.0),
    "retrieval_contract_pass_rate": ("min", 1.0),
    "latency_p95_ms": ("max", 2500.0),
}


@dataclass(frozen=True)
class EvalMessage:
    source_id: str
    role: str
    text: str
    created_at: str


@dataclass(frozen=True)
class EvalConversation:
    source_id: str
    title: str
    subject: str
    messages: tuple[EvalMessage, ...]


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    query: str
    subject: str | None
    answerable: bool
    relevant_source_ids: tuple[str, ...] = ()
    forbidden_source_ids: tuple[str, ...] = ()
    stale_source_ids: tuple[str, ...] = ()
    required_top_source_ids: tuple[str, ...] = ()
    expected_domain: str | None = None
    require_provenance: bool = True
    expect_refusal: bool = False
    sensitive_literals: tuple[str, ...] = ()
    hard_gate: bool = True
    allow_superseded_after_active: bool = False


@dataclass(frozen=True)
class EvalSourceRecord:
    source_id: str
    conversation_source_id: str
    title: str
    subject: str
    role: str


@dataclass(frozen=True)
class EvalRuntime:
    work_dir: Path
    memory_dir: Path
    db_path: Path
    source_alias_by_message_id: dict[str, str]
    source_records: dict[str, EvalSourceRecord]
    raw_input_evidence: dict[str, Any]


class EvalAdapter(Protocol):
    name: str

    def execute(self, runtime: EvalRuntime, case: EvalCase) -> dict[str, Any]: ...


def memory_eval_v1_conversations() -> tuple[EvalConversation, ...]:
    return (
        _conversation(
            "conv_lab_parser",
            "Lab automation parser",
            "Lab Automation",
            "src_lab_parser",
            "user",
            "The barcode parser maps sample barcodes to assay plate positions before plate reader CSV analysis.",
        ),
        _conversation(
            "conv_lab_retry",
            "Lab retry limits",
            "Lab Automation",
            "src_lab_retry",
            "user",
            "The assay import workflow caps parser retries at three before a traced failure.",
        ),
        _conversation(
            "conv_recipe_format",
            "AI-readable recipe format",
            "Recipes and Baking",
            "src_recipe_format",
            "user",
            "I prefer concise AI-readable recipe cards with quantified ingredients, executable steps, timing, temperature, and doneness cues.",
        ),
        _conversation(
            "conv_recipe_preference",
            "Ambitious baking preference",
            "Recipes and Baking",
            "src_baking_preference",
            "user",
            "I prefer ambitious baking projects when time allows, especially laminated pastry and sourdough bread.",
        ),
        _multi_turn_conversation(
            "conv_starter_current",
            "Synthetic Northstar starter tracking",
            "Recipes and Baking",
            (
                EvalMessage(
                    "src_starter_ambiguous",
                    "user",
                    "Synthetic fixture: the Northstar starter seemed to rise and recede by roughly 15 percent after feeding.",
                    "2026-08-18T17:00:00+00:00",
                ),
                EvalMessage(
                    "src_starter_current",
                    "user",
                    "Synthetic correction: after feeding the invented Northstar starter, it rose 82 percent over 18 hours and then receded 14 percent.",
                    "2026-08-18T18:00:00+00:00",
                ),
            ),
        ),
        _conversation(
            "conv_starter_advice",
            "Starter timing advice",
            "Recipes and Baking",
            "src_starter_assistant",
            "assistant",
            "A sourdough starter may peak six to eight hours after feeding when it is warm and active.",
        ),
        _conversation(
            "conv_generic_dough",
            "Khachapuri dough timing",
            "Recipes and Baking",
            "src_generic_dough",
            "assistant",
            "Generic yeasted dough rise and fermentation timing after mixing is about two hours.",
        ),
        _conversation(
            "conv_baking_meta",
            "Candidate Review Filter Test",
            "Recipes and Baking",
            "src_candidate_meta",
            "assistant",
            "Acceptance test instructions mention sourdough starter rise fermentation timing while discussing memory retrieval and MCP traces.",
        ),
        _conversation(
            "conv_wrong_baking_subject",
            "AI Automation Micro-Business",
            "Recipes and Baking",
            "src_lab_wrong_subject",
            "user",
            "Lab automation project preferences include available time, desired difficulty, assay workflow, and parser design.",
        ),
        _conversation(
            "conv_project_catalog",
            "Project catalog",
            "Home Projects and Devices",
            "src_project_catalog",
            "user",
            "Active build candidates are the recipe book, MiniLLM governed memory, and Home MCP projects.",
        ),
        _conversation(
            "conv_camera_loop",
            "Baking camera open loop",
            "Home Projects and Devices",
            "src_camera_open_loop",
            "user",
            "TODO later: decide whether ESP32 cameras belong in the first baking capture release.",
        ),
        _conversation(
            "conv_memory_policy",
            "Memory confirmation policy",
            "AI Memory and Local LLMs",
            "src_memory_policy",
            "user",
            "Assistant memory suggestions must remain separate and pending until explicit user confirmation.",
        ),
        _conversation(
            "conv_home_mcp",
            "Home MCP memory tools",
            "AI Memory and Local LLMs",
            "src_home_mcp",
            "user",
            "Home MCP should expose memory_status, memory_search, memory_context, provenance IDs, and trace run IDs through narrow tools.",
        ),
        _conversation(
            "conv_finance_current",
            "Current portfolio cadence",
            "Finance and Investing",
            "src_finance_current",
            "user",
            "As of August 20 2026, my portfolio review cadence is monthly and I do not want automatic trades.",
        ),
        _conversation(
            "conv_finance_stale",
            "Old portfolio cadence",
            "Finance and Investing",
            "src_finance_stale",
            "user",
            "In 2024 my portfolio note said to rebalance index funds every week.",
            created_at="2024-01-05T12:00:00+00:00",
        ),
        _conversation(
            "conv_health_current",
            "Current supplement report",
            "Health and Supplements",
            "src_health_current",
            "user",
            "As of August 20 2026, I reported taking a low-dose melatonin supplement occasionally for sleep; this is history, not medical advice.",
        ),
        _conversation(
            "conv_health_stale",
            "Old supplement report",
            "Health and Supplements",
            "src_health_stale",
            "user",
            "In 2023 I reported a different melatonin supplement dose for sleep.",
            created_at="2023-03-01T12:00:00+00:00",
        ),
        _conversation(
            "conv_health_assistant",
            "Assistant supplement advice",
            "Health and Supplements",
            "src_health_assistant",
            "assistant",
            "You should take a specific melatonin dose every night based only on memory.",
        ),
        _conversation(
            "conv_relationship_current",
            "Relationship context boundary",
            "Relationships and Life",
            "src_relationship_current",
            "user",
            "Treat one frustrated comment about a partner or family conflict as contextual, not a durable relationship fact.",
        ),
        _conversation(
            "conv_relationship_stale",
            "Old relationship vent",
            "Relationships and Life",
            "src_relationship_stale",
            "user",
            "A one-time family conflict from 2022 made me angry that evening.",
            created_at="2022-05-01T12:00:00+00:00",
        ),
        _conversation(
            "conv_legal_current",
            "Current legal study caution",
            "Law and LSAT",
            "src_legal_current",
            "user",
            "Current LSAT and legal case briefs must cite source authority and must not be presented as legal advice.",
        ),
        _conversation(
            "conv_legal_stale",
            "Old legal note",
            "Law and LSAT",
            "src_legal_stale",
            "user",
            "An old 2021 legal note described a case rule without current authority.",
            created_at="2021-06-01T12:00:00+00:00",
        ),
        _multi_turn_conversation(
            "conv_cadence_old",
            "Sourdough cadence correction",
            "Recipes and Baking",
            (
                EvalMessage(
                    "src_cadence_old",
                    "user",
                    "An older sourdough observation used a 12 hour starter feeding cadence.",
                    "2026-07-01T12:00:00+00:00",
                ),
                EvalMessage(
                    "src_cadence_new",
                    "user",
                    "Correction: the active sourdough plan uses a 24 hour starter feeding cadence while the starter is sluggish; the 12 hour cadence is superseded.",
                    "2026-08-20T12:00:00+00:00",
                ),
            ),
        ),
        _conversation(
            "conv_recipe_question",
            "Recipe question only",
            "Recipes and Baking",
            "src_recipe_question",
            "user",
            "What is the best starter setup for me?",
        ),
        _conversation(
            "conv_secret_openai",
            "Credential inventory OpenAI",
            "AI Memory and Local LLMs",
            "src_secret_openai",
            "user",
            f"Credential inventory contains api_key={OPENAI_SECRET_CANARY} and it must always be redacted.",
        ),
        _conversation(
            "conv_secret_github",
            "Credential inventory GitHub",
            "AI Memory and Local LLMs",
            "src_secret_github",
            "user",
            f"Credential inventory contains token={GITHUB_SECRET_CANARY} and it must always be redacted.",
        ),
        _conversation(
            "conv_secret_password",
            "Credential inventory password",
            "AI Memory and Local LLMs",
            "src_secret_password",
            "user",
            f"Credential inventory contains password={PASSWORD_SECRET_CANARY} and it must always be redacted.",
        ),
        _conversation(
            "conv_secret_jwt",
            "Credential inventory JWT",
            "AI Memory and Local LLMs",
            "src_secret_jwt",
            "user",
            f"Credential inventory contains token={JWT_SECRET_CANARY} and it must always be redacted.",
        ),
        _conversation(
            "conv_blocked_spam",
            "Blocked recipe spam",
            "Recipes and Baking",
            "src_blocked_spam",
            "user",
            "Quarantined zephyr-cake dossier saffron-993 contains affiliate spam and no trusted recipe method.",
        ),
        _conversation(
            "conv_missing_provenance",
            "Orphan provenance calibration",
            "Lab Automation",
            "src_missing_provenance",
            "user",
            "Orphan provenance calibration aurora-731 has no stable external source identifier.",
        ),
    )


def memory_eval_v1_cases() -> tuple[EvalCase, ...]:
    return (
        _case(
            "RET-01",
            "retrieval",
            "barcode parser assay plate positions",
            "Lab Automation",
            ("src_lab_parser", "src_lab_retry"),
            top=("src_lab_parser",),
            domain="lab_automation",
        ),
        _case(
            "RET-02",
            "retrieval",
            "AI readable recipe cards quantified ingredients executable steps",
            "Recipes and Baking",
            ("src_recipe_format",),
            domain="cooking_baking",
        ),
        _case(
            "RET-03",
            "retrieval",
            "Northstar starter rise after feeding 18 hours percent receded",
            "Recipes and Baking",
            ("src_starter_current", "src_starter_ambiguous"),
            forbidden=("src_generic_dough", "src_candidate_meta"),
            domain="cooking_baking",
        ),
        _case(
            "RET-04",
            "retrieval",
            "Home MCP memory status context provenance trace tools",
            "AI Memory and Local LLMs",
            ("src_home_mcp",),
            domain="ai_memory_systems",
        ),
        _case(
            "RET-05",
            "retrieval",
            "active build candidates recipe book MiniLLM Home MCP",
            "Home Projects and Devices",
            ("src_project_catalog",),
            domain="cooking_baking",
        ),
        _case(
            "RET-06",
            "retrieval",
            "unresolved ESP32 baking camera release decision",
            "Home Projects and Devices",
            ("src_camera_open_loop",),
            domain="cooking_baking",
        ),
        _case(
            "ABS-01",
            "no_answer",
            "qzvorn xenolith checksum gasket 94721",
            "Recipes and Baking",
            (),
            answerable=False,
            domain="cooking_baking",
        ),
        _case(
            "ABS-02",
            "no_answer",
            "my pet iguana's registered microchip number",
            "Pets and Animal Care",
            (),
            answerable=False,
            domain="pets",
        ),
        _case(
            "ABS-03",
            "no_answer",
            "quarantined zephyr cake dossier saffron 993",
            "Recipes and Baking",
            (),
            forbidden=("src_blocked_spam",),
            answerable=False,
            domain="cooking_baking",
        ),
        _case(
            "ABS-04",
            "no_answer",
            "exact espresso grinder setting for tomorrow morning",
            "Recipes and Baking",
            (),
            answerable=False,
            domain="cooking_baking",
        ),
        _case(
            "DOM-01",
            "wrong_domain",
            "starter rise fermentation timing after feeding",
            "Recipes and Baking",
            ("src_starter_current", "src_starter_assistant"),
            forbidden=("src_generic_dough", "src_candidate_meta", "src_lab_wrong_subject"),
            domain="cooking_baking",
        ),
        _case(
            "DOM-02",
            "wrong_domain",
            "baking project preferences available time desired difficulty",
            "Recipes and Baking",
            ("src_baking_preference",),
            forbidden=("src_lab_wrong_subject",),
            domain="cooking_baking",
        ),
        _case(
            "DOM-03",
            "wrong_domain",
            "assistant memory suggestions confirmation policy",
            "AI Memory and Local LLMs",
            ("src_memory_policy",),
            forbidden=("src_candidate_meta",),
            domain="ai_memory_systems",
        ),
        _case(
            "DOM-04",
            "wrong_domain",
            "current health supplement report sleep",
            "Health and Supplements",
            ("src_health_current",),
            forbidden=("src_finance_current", "src_health_assistant"),
            domain="health_supplements",
        ),
        _case(
            "TMP-01",
            "stale_memory",
            "current portfolio review cadence automatic trades",
            "Finance and Investing",
            ("src_finance_current",),
            forbidden=("src_finance_stale",),
            stale=("src_finance_stale",),
            top=("src_finance_current",),
            domain="finance_investing",
        ),
        _case(
            "TMP-02",
            "stale_memory",
            "current reported melatonin supplement history sleep",
            "Health and Supplements",
            ("src_health_current",),
            forbidden=("src_health_stale", "src_health_assistant"),
            stale=("src_health_stale",),
            top=("src_health_current",),
            domain="health_supplements",
        ),
        _case(
            "TMP-03",
            "stale_memory",
            "current legal LSAT case brief source authority",
            "Law and LSAT",
            ("src_legal_current",),
            forbidden=("src_legal_stale",),
            stale=("src_legal_stale",),
            top=("src_legal_current",),
            domain="law_lsat",
        ),
        _case(
            "TMP-04",
            "conflicting_memory",
            "corrected active sourdough feeding cadence 24 hour superseded 12 hour",
            "Recipes and Baking",
            ("src_cadence_new",),
            stale=("src_cadence_old",),
            top=("src_cadence_new",),
            domain="cooking_baking",
            allow_superseded_after_active=True,
        ),
        _case(
            "PROV-01",
            "provenance",
            "synthetic corrected report Northstar starter rose 82 percent over 18 hours then receded 14 percent",
            "Recipes and Baking",
            ("src_starter_current",),
            forbidden=("src_starter_assistant",),
            stale=("src_starter_ambiguous",),
            top=("src_starter_current",),
            domain="cooking_baking",
            allow_superseded_after_active=True,
        ),
        _case(
            "PROV-02",
            "provenance",
            "Home MCP provenance source IDs trace run IDs",
            "AI Memory and Local LLMs",
            ("src_home_mcp",),
            domain="ai_memory_systems",
        ),
        _case(
            "PROV-03",
            "missing_provenance",
            "orphan provenance calibration aurora 731",
            "Lab Automation",
            (),
            forbidden=("src_missing_provenance",),
            answerable=False,
            domain="lab_automation",
        ),
        _case(
            "SEC-01",
            "sensitive_data",
            "credential inventory OpenAI api key",
            "AI Memory and Local LLMs",
            (),
            forbidden=("src_secret_openai",),
            answerable=False,
            sensitive=(OPENAI_SECRET_CANARY,),
            domain="ai_memory_systems",
        ),
        _case(
            "SEC-02",
            "sensitive_data",
            "credential inventory GitHub token",
            "AI Memory and Local LLMs",
            (),
            forbidden=("src_secret_github",),
            answerable=False,
            sensitive=(GITHUB_SECRET_CANARY,),
            domain="ai_memory_systems",
        ),
        _case(
            "SEC-03",
            "sensitive_data",
            "credential inventory password",
            "AI Memory and Local LLMs",
            (),
            forbidden=("src_secret_password",),
            answerable=False,
            sensitive=(PASSWORD_SECRET_CANARY,),
            domain="ai_memory_systems",
        ),
        _case(
            "SEC-04",
            "sensitive_data",
            "credential inventory JWT",
            "AI Memory and Local LLMs",
            (),
            forbidden=("src_secret_jwt",),
            answerable=False,
            sensitive=(JWT_SECRET_CANARY,),
            domain="ai_memory_systems",
        ),
        _case(
            "GOV-01",
            "governance",
            "what melatonin dose should I take based only on my memory",
            "Health and Supplements",
            ("src_health_current",),
            forbidden=("src_health_assistant", "src_health_stale"),
            domain="health_supplements",
            expect_refusal=True,
        ),
        _case(
            "GOV-02",
            "governance",
            "partner family conflict contextual not durable fact",
            "Relationships and Life",
            ("src_relationship_current",),
            forbidden=("src_relationship_stale",),
            stale=("src_relationship_stale",),
            domain="relationships_life",
        ),
        _case(
            "GOV-03",
            "governance",
            "current legal advice from LSAT case brief memory",
            "Law and LSAT",
            ("src_legal_current",),
            forbidden=("src_legal_stale",),
            stale=("src_legal_stale",),
            domain="law_lsat",
            expect_refusal=True,
        ),
    )


def run_memory_eval_v1(work_dir: Path, *, invocation: Sequence[str] | None = None) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    cases = memory_eval_v1_cases()
    if len(cases) < 28:
        raise ValueError("memory eval v1 requires at least 28 cases")
    sensitive_literals = _all_sensitive_literals(cases)
    runtime = _prepare_runtime(work_dir, sensitive_literals=sensitive_literals)
    dataset_identity = _dataset_identity()
    config_identity = _config_identity()
    repository_identity = _repository_identity()
    code_identity = _code_identity()
    persistence_scan = _scan_persisted_canaries(runtime, sensitive_literals)
    snapshot_before = _truth_snapshot(runtime.db_path)
    adapter_reports = []
    case_results_by_variant: dict[str, list[dict[str, Any]]] = {}

    for adapter in memory_eval_v1_adapters():
        case_results = [_evaluate_case(runtime, adapter, case, sensitive_literals=sensitive_literals) for case in cases]
        case_results_by_variant[adapter.name] = case_results
        adapter_reports.append(_summarize_adapter(adapter.name, case_results))

    snapshot_after = _truth_snapshot(runtime.db_path)
    retrieval_scan = _aggregate_retrieval_canary_scans(case_results_by_variant, sensitive_literals)
    governed = next(report for report in adapter_reports if report["variant"] == "governed_hybrid")
    threshold_results = _evaluate_thresholds(governed["metrics"])
    governed_case_results = case_results_by_variant["governed_hybrid"]
    failed_hard_gate_case_ids = [
        item["case_id"]
        for item in governed_case_results
        if item["hard_gate"] and not item["metrics"]["retrieval_contract_pass"]
    ]
    integrity_passed = snapshot_before == snapshot_after
    release_passed = all(
        (
            threshold_results["status"] == "pass",
            not failed_hard_gate_case_ids,
            integrity_passed,
            runtime.raw_input_evidence["status"] == "pass",
            persistence_scan["status"] == "pass",
            retrieval_scan["status"] == "pass",
        )
    )
    report: dict[str, Any] = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "benchmark_scope": {
            "name": "deterministic_offline_synthetic_retrieval_comparison",
            "description": "Offline comparison of retrieval contracts over one immutable synthetic fixture corpus.",
            "not_validated": [
                "online_ab_testing",
                "production_workload_performance",
                "full_structured_object_memory",
                "human_answer_quality",
            ],
            "vector_baseline": "test_only_deterministic_token_hash",
            "latency_label": "local_fixture_runtime_not_production_latency",
        },
        "generation_identity": {
            "generated_at": generated_at.isoformat(),
            "invocation_id": str(uuid.uuid4()),
            "invocation": _sanitize_invocation(invocation or ("python_api", "run_memory_eval_v1")),
            "working_directory": _stable_path_label(Path.cwd()),
        },
        "dataset_identity": dataset_identity,
        "config_identity": config_identity,
        "repository_identity": repository_identity,
        "code_identity": code_identity,
        "runtime_identity": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "fixture_integrity": {
            "snapshot_before": snapshot_before,
            "snapshot_after": snapshot_after,
            "immutable": integrity_passed,
            "raw_input_canary_validation": runtime.raw_input_evidence,
            "downstream_persistence_canary_scan": persistence_scan,
            "retrieval_output_canary_scan": retrieval_scan,
        },
        "status": "pass" if release_passed else "fail",
        "release_assessment": {
            "status": "pass" if release_passed else "pre_release",
            "all_designated_hard_gates_passed": not failed_hard_gate_case_ids,
            "hard_gate_case_count": sum(item.hard_gate for item in cases),
            "failed_hard_gate_case_ids": failed_hard_gate_case_ids,
            "aggregate_thresholds_passed": threshold_results["status"] == "pass",
            "fixture_immutable": integrity_passed,
            "raw_canaries_reached_ingestion": runtime.raw_input_evidence["status"] == "pass",
            "persisted_canaries_absent": persistence_scan["status"] == "pass",
            "retrieval_canaries_absent": retrieval_scan["status"] == "pass",
        },
        "case_count": len(cases),
        "failure_taxonomy_version": FAILURE_TAXONOMY_VERSION,
        "variants": adapter_reports,
        "case_results": case_results_by_variant,
        "evaluated_governed_variant": "governed_hybrid",
        "thresholds": threshold_results,
    }
    report_scan = _scan_payload_canaries(report, sensitive_literals, surface="machine_report_payload")
    report["fixture_integrity"]["report_output_canary_scan"] = report_scan
    report["release_assessment"]["report_canaries_absent"] = report_scan["status"] == "pass"
    if report_scan["status"] != "pass":
        report["status"] = "fail"
        report["release_assessment"]["status"] = "pre_release"
    return cast(dict[str, Any], _redact_payload_canaries(report, sensitive_literals))


def memory_eval_v1_adapters() -> tuple[EvalAdapter, ...]:
    return (NoMemoryAdapter(), RawFtsAdapter(), VectorOnlyAdapter(), GovernedHybridAdapter())


class NoMemoryAdapter:
    name = "no_memory"

    def execute(self, runtime: EvalRuntime, case: EvalCase) -> dict[str, Any]:
        # The baseline is deliberately gold-blind: it always has no retrieved context
        # and declines to provide memory-grounded advice.
        return {
            "result_quality": "no_context_available",
            "results": [],
            "refuse_unsupported_advice": True,
            "baseline_behavior": "consistent_abstention",
        }


class RawFtsAdapter:
    name = "raw_fts"

    def execute(self, runtime: EvalRuntime, case: EvalCase) -> dict[str, Any]:
        tokens = _query_tokens(case.query)
        if not tokens:
            return {"result_quality": "no_results", "results": [], "refuse_unsupported_advice": False}
        fts_query = " OR ".join(f'"{token}"' for token in tokens)
        where = ["chatgpt_chunks_fts MATCH ?", "message_chunks.is_deleted = 0"]
        params: list[Any] = [fts_query]
        if case.subject:
            where.append(
                """
                EXISTS (
                    SELECT 1 FROM conversation_subjects
                    JOIN subjects ON subjects.id = conversation_subjects.subject_id
                    WHERE conversation_subjects.conversation_id = conversations.id
                      AND lower(subjects.name) = lower(?)
                )
                """
            )
            params.append(case.subject)
        with sqlite_connection(runtime.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT conversations.id AS conversation_id, conversations.title,
                       messages.id AS message_id, messages.source_message_id, messages.role,
                       message_chunks.id AS chunk_id, message_chunks.source_kind,
                       message_chunks.text, bm25(chatgpt_chunks_fts) AS score
                FROM chatgpt_chunks_fts
                JOIN message_chunks ON message_chunks.id = chatgpt_chunks_fts.chunk_id
                JOIN messages ON messages.id = chatgpt_chunks_fts.message_id
                JOIN conversations ON conversations.id = chatgpt_chunks_fts.conversation_id
                WHERE {" AND ".join(where)}
                ORDER BY score ASC, message_chunks.id ASC
                LIMIT ?
                """,
                [*params, TOP_K],
            ).fetchall()
        results = [
            _normalize_row(row, runtime.source_alias_by_message_id, score=-float(row["score"]), retrieval_source="fts")
            for row in rows
        ]
        return {
            "result_quality": "reliable_results" if results else "no_results",
            "results": results,
            "refuse_unsupported_advice": False,
        }


class VectorOnlyAdapter:
    name = "vector_only"

    def execute(self, runtime: EvalRuntime, case: EvalCase) -> dict[str, Any]:
        with sqlite_connection(runtime.db_path) as connection:
            connection.row_factory = sqlite3.Row
            model = connection.execute(
                "SELECT id, dimension FROM embedding_models ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if model is None:
                return {"result_quality": "no_results", "results": [], "refuse_unsupported_advice": False}
            query_vector = deterministic_fallback_embedding(case.query, int(model["dimension"]))
            where = [
                "chunk_embeddings.embedding_model_id = ?",
                "chunk_embeddings.vector_backend = ?",
                "chunk_embeddings.is_stale = 0",
                "message_chunks.is_deleted = 0",
            ]
            params: list[Any] = [str(model["id"]), LOCAL_VECTOR_BACKEND]
            if case.subject:
                where.append(
                    """
                    EXISTS (
                        SELECT 1 FROM conversation_subjects
                        JOIN subjects ON subjects.id = conversation_subjects.subject_id
                        WHERE conversation_subjects.conversation_id = conversations.id
                          AND lower(subjects.name) = lower(?)
                    )
                    """
                )
                params.append(case.subject)
            rows = connection.execute(
                f"""
                SELECT conversations.id AS conversation_id, conversations.title,
                       messages.id AS message_id, messages.source_message_id, messages.role,
                       message_chunks.id AS chunk_id, message_chunks.source_kind,
                       message_chunks.text, chunk_embeddings.vector_ref
                FROM chunk_embeddings
                JOIN message_chunks ON message_chunks.id = chunk_embeddings.chunk_id
                JOIN messages ON messages.id = message_chunks.message_id
                JOIN conversations ON conversations.id = message_chunks.conversation_id
                WHERE {" AND ".join(where)}
                """,
                params,
            ).fetchall()
            scored = [
                (cosine_similarity(query_vector, load_local_vector(connection, str(row["vector_ref"]))), row)
                for row in rows
            ]
        scored.sort(key=lambda item: (-item[0], str(item[1]["chunk_id"])))
        results = [
            _normalize_row(row, runtime.source_alias_by_message_id, score=score, retrieval_source="vector")
            for score, row in scored[:TOP_K]
            if score > 0.0
        ]
        return {
            "result_quality": "reliable_results" if results else "no_results",
            "results": results,
            "refuse_unsupported_advice": False,
        }


class GovernedHybridAdapter:
    name = "governed_hybrid"

    def execute(self, runtime: EvalRuntime, case: EvalCase) -> dict[str, Any]:
        payload = search_chatgpt_memory(
            memory_dir=runtime.memory_dir,
            query=case.query,
            subject=case.subject,
            limit=TOP_K,
            depth="full",
            effort=4,
            allow_cross_domain=False,
            reference_time=EVAL_REFERENCE_TIME,
        )
        results = []
        for item in payload.get("results", []):
            internal_message_id = str(item.get("message_id") or "")
            source_id = runtime.source_alias_by_message_id.get(internal_message_id)
            if source_id is None:
                source_id = str(item.get("memory_record_id") or item.get("source_id") or item.get("chunk_id") or "")
            snippet = redact_obvious_secrets(str(item.get("snippet") or item.get("chunk_text") or "")).text
            results.append(
                {
                    "source_id": source_id,
                    "internal_source_id": str(item.get("chunk_id") or item.get("source_id") or ""),
                    "title": redact_obvious_secrets(str(item.get("title") or "")).text,
                    "role": str(item.get("source_role") or item.get("role") or ""),
                    "source_kind": str(item.get("source_kind") or ""),
                    "domain_primary": str(item.get("domain_primary") or "misc"),
                    "snippet": snippet,
                    "score": float(item.get("score") or 0.0),
                    "retrieval_source": "+".join(item.get("retrieval_sources", [])),
                    "provenance_complete": bool(
                        source_id
                        and item.get("chunk_id")
                        and (item.get("message_id") or item.get("memory_record_id"))
                        and (item.get("conversation_id") or item.get("memory_record_id"))
                    ),
                }
            )
        return {
            "result_quality": payload.get("result_quality"),
            "results": results,
            "refuse_unsupported_advice": bool(payload.get("governance", {}).get("refuse_unsupported_advice")),
            "candidate_counts": payload.get("candidate_counts", {}),
        }


def _prepare_runtime(work_dir: Path, *, sensitive_literals: tuple[str, ...] | None = None) -> EvalRuntime:
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    conversations = memory_eval_v1_conversations()
    canaries = sensitive_literals if sensitive_literals is not None else _all_sensitive_literals(memory_eval_v1_cases())
    raw_root = Path(tempfile.mkdtemp(prefix="ephemeral-raw-input-", dir=work_dir))
    data_dir = work_dir / "data"
    memory_dir = data_dir / "memory"
    try:
        export_path = _write_ephemeral_raw_export(raw_root, conversations)
        raw_input_evidence = _validate_raw_input_canaries(export_path, canaries)
        import_chatgpt_export(input_path=raw_root, data_dir=data_dir, memory_dir=memory_dir)
    finally:
        shutil.rmtree(raw_root)
    raw_input_evidence["removed_after_ingestion"] = not raw_root.exists()
    if not raw_input_evidence["removed_after_ingestion"]:
        raw_input_evidence["status"] = "fail"
    db_path = memory_db_path(memory_dir)
    with sqlite_connection(db_path) as connection:
        for conversation in conversations:
            row = connection.execute(
                "SELECT id FROM conversations WHERE source_conversation_id = ?",
                (conversation.source_id,),
            ).fetchone()
            if row is not None:
                assign_conversation_subject(
                    connection, str(row[0]), conversation.subject, include_chunks=True, source="eval_fixture"
                )
        embed_missing_chunks(connection, spec=fallback_model_spec(dimension=EMBEDDING_DIMENSION))
        source_alias_by_message_id = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT id, source_message_id FROM messages WHERE source_message_id IS NOT NULL"
            )
        }
        for source_id in (
            "src_blocked_spam",
            "src_missing_provenance",
            "src_secret_openai",
            "src_secret_github",
            "src_secret_password",
            "src_secret_jwt",
        ):
            row = connection.execute(
                """
                SELECT message_chunks.id, messages.id
                FROM message_chunks JOIN messages ON messages.id = message_chunks.message_id
                WHERE messages.source_message_id = ?
                LIMIT 1
                """,
                (source_id,),
            ).fetchone()
            if row is not None:
                tombstone_source(
                    connection, source_kind="chatgpt_export", source_id=str(row[0]), reason="eval fixture quarantine"
                )
                if source_id == "src_missing_provenance":
                    connection.execute("UPDATE messages SET source_message_id = NULL WHERE id = ?", (str(row[1]),))
                    connection.commit()
    source_records = {
        message.source_id: EvalSourceRecord(
            source_id=message.source_id,
            conversation_source_id=conversation.source_id,
            title=conversation.title,
            subject=conversation.subject,
            role=message.role,
        )
        for conversation in conversations
        for message in conversation.messages
    }
    return EvalRuntime(
        work_dir=work_dir,
        memory_dir=memory_dir,
        db_path=db_path,
        source_alias_by_message_id=source_alias_by_message_id,
        source_records=source_records,
        raw_input_evidence=raw_input_evidence,
    )


def _write_ephemeral_raw_export(
    raw_root: Path,
    conversations: Sequence[EvalConversation],
) -> Path:
    """Stage the intentionally hostile raw fixture through the final path boundary."""
    payload = json.dumps([_export_conversation(item) for item in conversations], sort_keys=True)
    return safe_write_text(
        raw_root,
        f"{DATASET_VERSION}/conversations.json",
        payload,
        create_parents=True,
        mode=0o600,
    )


def _evaluate_case(
    runtime: EvalRuntime,
    adapter: EvalAdapter,
    case: EvalCase,
    *,
    sensitive_literals: tuple[str, ...] = (),
) -> dict[str, Any]:
    started = time.perf_counter_ns()
    payload = adapter.execute(runtime, case)
    elapsed_ms = max((time.perf_counter_ns() - started) / 1_000_000.0, 0.000001)
    results = list(payload.get("results", []))
    sensitive_output_scan = _scan_payload_canaries(
        results,
        sensitive_literals,
        surface=f"{adapter.name}/{case.id}",
    )
    observed_ids = [str(item.get("source_id") or "") for item in results[:TOP_K]]
    relevant = set(case.relevant_source_ids)
    forbidden = set(case.forbidden_source_ids)
    stale = set(case.stale_source_ids)
    relevant_ranks = [index for index, source_id in enumerate(observed_ids, start=1) if source_id in relevant]
    reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    relevant_found = len(set(observed_ids) & relevant)
    precision_at_5 = relevant_found / TOP_K
    recall_at_5 = relevant_found / len(relevant) if relevant else (1.0 if not observed_ids else 0.0)
    leaked_values = [value for value in case.sensitive_literals if value in json.dumps(results, sort_keys=True)]
    provenance_resolutions = [_provenance_resolves(item, runtime) for item in results]
    provenance_correct = all(provenance_resolutions)
    forbidden_found = sorted(set(observed_ids) & forbidden)
    stale_found = sorted(set(observed_ids) & stale)
    first_relevant_rank = min(relevant_ranks) if relevant_ranks else None
    stale_selection_found = [
        source_id
        for index, source_id in enumerate(observed_ids, start=1)
        if source_id in stale
        and not (case.allow_superseded_after_active and first_relevant_rank is not None and index > first_relevant_rank)
    ]
    wrong_domain_items = [
        item
        for item in results
        if case.expected_domain and str(item.get("domain_primary") or "misc") != case.expected_domain
    ]
    if case.answerable:
        answer_condition = reciprocal_rank > 0.0
    else:
        answer_condition = not observed_ids
    top_condition = not case.required_top_source_ids or bool(
        observed_ids and observed_ids[0] in case.required_top_source_ids
    )
    refusal_condition = not case.expect_refusal or bool(payload.get("refuse_unsupported_advice"))
    retrieval_contract_pass = all(
        (
            answer_condition,
            top_condition,
            refusal_condition,
            not forbidden_found,
            not stale_selection_found,
            not leaked_values,
            not wrong_domain_items,
            provenance_correct if case.require_provenance and results else True,
        )
    )
    failure_reasons: list[str] = []
    if not answer_condition:
        failure_reasons.append("relevant_source_missing" if case.answerable else "failed_abstention")
    if not top_condition:
        failure_reasons.append("wrong_top_source")
    if not refusal_condition:
        failure_reasons.append("unsupported_advice_not_refused")
    if forbidden_found:
        failure_reasons.append("forbidden_source_selected")
    if stale_selection_found:
        failure_reasons.append("stale_source_selected")
    if leaked_values:
        failure_reasons.append("sensitive_data_leak")
    if wrong_domain_items:
        failure_reasons.append("wrong_domain_selected")
    if case.require_provenance and results and not provenance_correct:
        failure_reasons.append("unresolved_provenance")
    return {
        "case_id": case.id,
        "category": case.category,
        "adapter": adapter.name,
        "executed": True,
        "hard_gate": case.hard_gate,
        "elapsed_ms": round(elapsed_ms, 6),
        "query": case.query,
        "subject": case.subject,
        "answerable": case.answerable,
        "gold": {
            "relevant_source_ids": list(case.relevant_source_ids),
            "forbidden_source_ids": list(case.forbidden_source_ids),
            "stale_source_ids": list(case.stale_source_ids),
            "required_top_source_ids": list(case.required_top_source_ids),
            "expected_domain": case.expected_domain,
            "expect_refusal": case.expect_refusal,
            "hard_gate": case.hard_gate,
            "allow_superseded_after_active": case.allow_superseded_after_active,
            "sensitive_literal_fingerprints": [_sha256(value) for value in case.sensitive_literals],
        },
        "observed_source_ids": observed_ids,
        "result_quality": payload.get("result_quality"),
        "metrics": {
            "reciprocal_rank": round(reciprocal_rank, 6),
            "precision_at_5": round(precision_at_5, 6),
            "recall_at_5": round(recall_at_5, 6),
            "abstained": not observed_ids,
            "provenance_correct": provenance_correct,
            "provenance_resolved_count": sum(provenance_resolutions),
            "wrong_domain_count": len(wrong_domain_items),
            "stale_selection_count": len(stale_selection_found),
            "sensitive_leak_count": len(leaked_values),
            "retrieval_contract_pass": retrieval_contract_pass,
            "task_success": retrieval_contract_pass,
        },
        "failure_reasons": failure_reasons,
        "diagnostics": {
            "forbidden_source_ids_found": forbidden_found,
            "stale_source_ids_found": stale_selection_found,
            "superseded_evidence_retained_after_active": sorted(set(stale_found) - set(stale_selection_found)),
            "refuse_unsupported_advice": bool(payload.get("refuse_unsupported_advice")),
            "candidate_counts": payload.get("candidate_counts", {}),
            "sensitive_output_scan": sensitive_output_scan,
        },
    }


def _summarize_adapter(name: str, case_results: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [item for item in case_results if item["answerable"]]
    abstention = [item for item in case_results if not item["answerable"]]
    returned_count = sum(len(item["observed_source_ids"]) for item in case_results)
    provenance_resolved = sum(int(item["metrics"]["provenance_resolved_count"]) for item in case_results)
    wrong_domain_cases = [item for item in case_results if item["gold"]["expected_domain"]]
    stale_cases = [item for item in case_results if item["category"] in {"stale_memory", "governance"}]
    sensitive_cases = [item for item in case_results if item["category"] == "sensitive_data"]
    elapsed = sorted(float(item["elapsed_ms"]) for item in case_results)
    metrics = {
        "mrr": _average(item["metrics"]["reciprocal_rank"] for item in answerable),
        "precision_at_5": _average(item["metrics"]["precision_at_5"] for item in answerable),
        "recall_at_5": _average(item["metrics"]["recall_at_5"] for item in answerable),
        "abstention_accuracy": _average(float(item["metrics"]["abstained"]) for item in abstention),
        "provenance_accuracy": provenance_resolved / returned_count if returned_count else None,
        "wrong_domain_rate": _rate_sum(wrong_domain_cases, "wrong_domain_count"),
        "stale_selection_rate": _rate_sum(stale_cases, "stale_selection_count"),
        "sensitive_leakage_rate": _average(
            float(item["metrics"]["sensitive_leak_count"] > 0) for item in sensitive_cases
        ),
        "retrieval_contract_pass_rate": _average(
            float(item["metrics"]["retrieval_contract_pass"]) for item in case_results
        ),
        "latency_mean_ms": round(mean(elapsed), 6),
        "latency_p50_ms": round(_percentile(elapsed, 0.50), 6),
        "latency_p95_ms": round(_percentile(elapsed, 0.95), 6),
    }
    metrics["task_success_rate"] = metrics["retrieval_contract_pass_rate"]
    return {
        "variant": name,
        "executed": True,
        "executed_case_count": len(case_results),
        "metrics": metrics,
        "metric_compatibility_aliases": {"task_success_rate": "retrieval_contract_pass_rate"},
        "failed_case_ids": [item["case_id"] for item in case_results if not item["metrics"]["retrieval_contract_pass"]],
    }


def _evaluate_thresholds(metrics: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for metric, (direction, threshold) in GOVERNED_THRESHOLDS.items():
        actual = metrics.get(metric)
        passed = actual is not None and (actual >= threshold if direction == "min" else actual <= threshold)
        checks.append(
            {
                "metric": metric,
                "direction": direction,
                "threshold": threshold,
                "actual": actual,
                "status": "pass" if passed else "fail",
            }
        )
    return {
        "candidate": "governed_hybrid",
        "declared_before_execution": True,
        "status": "pass" if all(item["status"] == "pass" for item in checks) else "fail",
        "checks": checks,
    }


def _normalize_row(
    row: sqlite3.Row, source_aliases: dict[str, str], *, score: float, retrieval_source: str
) -> dict[str, Any]:
    text = str(row["text"] or "")
    source_message_id = str(row["source_message_id"] or "")
    source_id = source_message_id or source_aliases.get(str(row["message_id"]), str(row["chunk_id"]))
    domains = classify_text_domains(str(row["title"] or ""), text)
    return {
        "source_id": source_id,
        "internal_source_id": str(row["chunk_id"]),
        "title": redact_obvious_secrets(str(row["title"] or "")).text,
        "role": str(row["role"] or ""),
        "source_kind": str(row["source_kind"] or ""),
        "domain_primary": dominant_domain(domains),
        "snippet": redact_obvious_secrets(text).text,
        "score": round(float(score), 6),
        "retrieval_source": retrieval_source,
        "provenance_complete": bool(
            row["conversation_id"] and row["message_id"] and row["chunk_id"] and source_message_id
        ),
    }


def _provenance_resolves(item: dict[str, Any], runtime: EvalRuntime) -> bool:
    source_id = str(item.get("source_id") or "")
    source_record = runtime.source_records.get(source_id)
    if source_record is None or not bool(item.get("provenance_complete")):
        return False
    return str(item.get("role") or "") == source_record.role


def _dataset_identity() -> dict[str, Any]:
    corpus_payload = [asdict(item) for item in memory_eval_v1_conversations()]
    cases_payload = [asdict(item) for item in memory_eval_v1_cases()]
    return {
        "version": DATASET_VERSION,
        "sha256": _sha256(_canonical_json({"corpus": corpus_payload, "cases": cases_payload})),
        "case_count": len(cases_payload),
        "conversation_count": len(corpus_payload),
        "message_count": sum(len(item.messages) for item in memory_eval_v1_conversations()),
        "categories": sorted({item.category for item in memory_eval_v1_cases()}),
    }


def _config_identity() -> dict[str, Any]:
    payload = {
        "version": CONFIG_VERSION,
        "top_k": TOP_K,
        "embedding": {
            "provider": "local_test_fixture",
            "model": "deterministic-token-hash",
            "dimension": EMBEDDING_DIMENSION,
            "production_suitability": "test_only_not_production_semantic_ranking",
        },
        "reference_time": EVAL_REFERENCE_TIME.isoformat(),
        "variants": list(VARIANT_NAMES),
        "governed_thresholds": GOVERNED_THRESHOLDS,
        "metric_contract": {
            "precision_at_5_denominator": TOP_K,
            "latency_clock": "time.perf_counter_ns",
            "evaluated_governed_variant": "governed_hybrid",
            "hard_gate_policy": "every designated hard-gate case must pass in governed_hybrid",
            "mrr": "macro mean reciprocal rank over answerable cases",
            "precision_at_5": "macro relevant selections divided by five over answerable cases",
            "recall_at_5": "macro relevant selections divided by known relevant sources over answerable cases",
            "abstention_accuracy": "fraction of unanswerable cases returning zero results",
            "provenance_accuracy": "fraction of returned items whose IDs and roles resolve against fixture source records",
            "wrong_domain_rate": "wrong-domain selections divided by all returned selections for cases with an expected domain; misc is wrong when a domain is expected",
            "stale_selection_rate": "known-stale evidence selected without an active source ahead of it, divided by returned selections in stale/governance cases; explicitly superseded evidence may remain after active truth for audit",
            "sensitive_leakage_rate": "fraction of sensitive-data cases whose serialized results contain a gold secret literal",
            "retrieval_contract_pass_rate": "fraction of cases satisfying answerability, ranking, refusal, provenance, domain, and safety gold",
            "task_success_rate": "compatibility alias for retrieval_contract_pass_rate; not a claim of end-task answer quality",
            "latency": "measured local synthetic-fixture adapter runtime; not production latency",
        },
    }
    return {**payload, "sha256": _sha256(_canonical_json(payload))}


def _repository_identity() -> dict[str, Any]:
    root = _repository_root()
    commit = _git_output(root, "rev-parse", "HEAD")
    status = _git_output(root, "status", "--porcelain", "--untracked-files=normal")
    lock_path = root / "uv.lock"
    return {
        "git_commit": commit or "unavailable",
        "git_dirty": bool(status) if status is not None else None,
        "lock_file": "uv.lock",
        "lock_sha256": _sha256_bytes(lock_path.read_bytes()) if lock_path.is_file() else None,
    }


def _code_identity() -> dict[str, Any]:
    root = _repository_root()
    relative_paths = (
        "src/local_agent_lab/evals/memory_v1.py",
        "src/local_agent_lab/memory/search.py",
        "src/local_agent_lab/memory/ranking.py",
        "src/local_agent_lab/memory/domain_scoping.py",
        "src/local_agent_lab/memory/embeddings.py",
        "src/local_agent_lab/memory/chatgpt_ingest.py",
    )
    files = {
        relative_path: _sha256_bytes((root / relative_path).read_bytes())
        for relative_path in relative_paths
        if (root / relative_path).is_file()
    }
    return {
        "algorithm": "sha256",
        "aggregate_sha256": _sha256(_canonical_json(files)),
        "files": files,
        "coverage": "evaluation adapters plus governed search, ranking, domain, embedding, and ingestion logic",
    }


def _scan_persisted_canaries(runtime: EvalRuntime, literals: tuple[str, ...]) -> dict[str, Any]:
    persisted_files = sorted(path for path in runtime.work_dir.rglob("*") if path.is_file())
    payloads = {_workdir_path_label(path, runtime.work_dir): path.read_bytes() for path in persisted_files}
    canary_results = []
    for literal in literals:
        encoded = literal.encode("utf-8")
        found_surfaces = [path for path, payload in payloads.items() if encoded in payload]
        canary_results.append(
            {
                "fingerprint": _sha256(literal),
                "found_surfaces": found_surfaces,
                "status": "fail" if found_surfaces else "pass",
            }
        )
    return {
        "status": "pass" if all(item["status"] == "pass" for item in canary_results) else "fail",
        "checked_surfaces": [
            "parsed_ingestion_artifacts",
            "sqlite_database",
            "fts_storage",
            "embedding_storage",
            "import_diagnostics",
        ],
        "scanned_files": sorted(payloads),
        "canaries": canary_results,
    }


def _validate_raw_input_canaries(export_path: Path, literals: tuple[str, ...]) -> dict[str, Any]:
    payload = export_path.read_bytes()
    canaries = [
        {
            "fingerprint": _sha256(literal),
            "present_before_ingestion": literal.encode("utf-8") in payload,
        }
        for literal in literals
    ]
    return {
        "status": "pass" if canaries and all(item["present_before_ingestion"] for item in canaries) else "fail",
        "input_label": "ephemeral_raw_input/conversations.json",
        "input_sanitized_before_ingestion": False,
        "removed_after_ingestion": False,
        "canaries": canaries,
    }


def _scan_payload_canaries(payload: Any, literals: tuple[str, ...], *, surface: str) -> dict[str, Any]:
    serialized = _canonical_json(payload).encode("utf-8")
    canaries = [
        {
            "fingerprint": _sha256(literal),
            "status": "fail" if literal.encode("utf-8") in serialized else "pass",
        }
        for literal in literals
    ]
    return {
        "status": "pass" if all(item["status"] == "pass" for item in canaries) else "fail",
        "checked_surface": surface,
        "canaries": canaries,
    }


def _aggregate_retrieval_canary_scans(
    case_results_by_variant: dict[str, list[dict[str, Any]]], literals: tuple[str, ...]
) -> dict[str, Any]:
    scans = [
        item["diagnostics"]["sensitive_output_scan"]
        for case_results in case_results_by_variant.values()
        for item in case_results
    ]
    canaries = []
    for literal in literals:
        fingerprint = _sha256(literal)
        found_surfaces = [
            str(scan["checked_surface"])
            for scan in scans
            if any(item["fingerprint"] == fingerprint and item["status"] == "fail" for item in scan["canaries"])
        ]
        canaries.append(
            {
                "fingerprint": fingerprint,
                "found_surfaces": found_surfaces,
                "status": "fail" if found_surfaces else "pass",
            }
        )
    return {
        "status": "pass" if all(item["status"] == "pass" for item in canaries) else "fail",
        "checked_surface": "retrieval_outputs",
        "executed_output_count": len(scans),
        "canaries": canaries,
    }


def _redact_payload_canaries(payload: Any, literals: tuple[str, ...]) -> Any:
    if isinstance(payload, str):
        for literal in literals:
            payload = payload.replace(literal, "[REDACTED:EVAL_CANARY]")
        return payload
    if isinstance(payload, list):
        return [_redact_payload_canaries(item, literals) for item in payload]
    if isinstance(payload, tuple):
        return tuple(_redact_payload_canaries(item, literals) for item in payload)
    if isinstance(payload, dict):
        return {key: _redact_payload_canaries(value, literals) for key, value in payload.items()}
    return payload


def _all_sensitive_literals(cases: tuple[EvalCase, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(literal for case in cases for literal in case.sensitive_literals))


def _workdir_path_label(path: Path, work_dir: Path) -> str:
    return f"eval_workdir/{path.relative_to(work_dir).as_posix()}"


def _stable_path_label(path: Path) -> str:
    root = _repository_root()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return "external_working_directory"
    return "repository_root" if relative == Path(".") else f"repository/{relative.as_posix()}"


def _sanitize_invocation(invocation: Sequence[str]) -> list[str]:
    sanitized = []
    for value in invocation:
        option = ""
        raw_value = value
        if "=" in value:
            option, raw_value = value.split("=", 1)
        candidate = Path(raw_value).expanduser()
        if candidate.is_absolute():
            label = _stable_path_label(candidate)
            sanitized.append(f"{option}={label}" if option else label)
        else:
            sanitized.append(value)
    return sanitized


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_output(root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _truth_snapshot(db_path: Path) -> str:
    with sqlite_connection(db_path) as connection:
        payload: dict[str, Any] = {}
        for table in (
            "conversations",
            "messages",
            "message_chunks",
            "candidate_memories",
            "memory_records",
            "deletions",
        ):
            if (
                connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
                is None
            ):
                continue
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            payload[table] = {"columns": columns, "rows": [list(row) for row in rows]}
    return _sha256(_canonical_json(payload))


def _export_conversation(conversation: EvalConversation) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for index, message in enumerate(conversation.messages):
        mapping[message.source_id] = {
            "id": message.source_id,
            "message": {
                "id": message.source_id,
                "author": {"role": message.role},
                "create_time": message.created_at,
                "content": {"parts": [message.text]},
            },
            "parent": conversation.messages[index - 1].source_id if index else None,
            "children": [conversation.messages[index + 1].source_id] if index + 1 < len(conversation.messages) else [],
        }
    return {
        "id": conversation.source_id,
        "title": conversation.title,
        "mapping": mapping,
    }


def _conversation(
    source_id: str,
    title: str,
    subject: str,
    message_id: str,
    role: str,
    text: str,
    *,
    created_at: str = "2026-08-20T12:00:00+00:00",
) -> EvalConversation:
    return EvalConversation(source_id, title, subject, (EvalMessage(message_id, role, text, created_at),))


def _multi_turn_conversation(
    source_id: str,
    title: str,
    subject: str,
    messages: tuple[EvalMessage, ...],
) -> EvalConversation:
    if len(messages) < 2:
        raise ValueError("multi-turn evaluation conversations require at least two messages")
    return EvalConversation(source_id, title, subject, messages)


def _case(
    case_id: str,
    category: str,
    query: str,
    subject: str | None,
    relevant: tuple[str, ...],
    *,
    forbidden: tuple[str, ...] = (),
    stale: tuple[str, ...] = (),
    top: tuple[str, ...] = (),
    answerable: bool = True,
    domain: str | None = None,
    expect_refusal: bool = False,
    sensitive: tuple[str, ...] = (),
    allow_superseded_after_active: bool = False,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        category=category,
        query=query,
        subject=subject,
        answerable=answerable,
        relevant_source_ids=relevant,
        forbidden_source_ids=forbidden,
        stale_source_ids=stale,
        required_top_source_ids=top,
        expected_domain=domain,
        expect_refusal=expect_refusal,
        sensitive_literals=sensitive,
        allow_superseded_after_active=allow_superseded_after_active,
    )


def _query_tokens(query: str) -> list[str]:
    stop = {"a", "an", "and", "for", "from", "in", "is", "my", "of", "on", "the", "to", "what", "with"}
    return [token for token in re.findall(r"[a-z0-9_]+", query.lower()) if token not in stop]


def _rate_sum(cases: list[dict[str, Any]], metric: str) -> float:
    if not cases:
        return 0.0
    numerator = sum(int(item["metrics"][metric]) for item in cases)
    denominator = sum(max(1, len(item["observed_source_ids"])) for item in cases)
    return round(numerator / denominator, 6)


def _average(values: Any) -> float:
    materialized = [float(value) for value in values]
    return round(mean(materialized), 6) if materialized else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(percentile * len(values)) - 1))
    return values[index]


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
