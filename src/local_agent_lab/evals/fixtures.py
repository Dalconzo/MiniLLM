from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryEvalMessage:
    id: str
    role: str
    text: str
    created_at: str | None = None


@dataclass(frozen=True)
class MemoryEvalConversation:
    id: str
    title: str
    messages: tuple[MemoryEvalMessage, ...]
    subject: str | None = None
    subject_kind: str = "subject"


@dataclass(frozen=True)
class UsagePromptCase:
    id: str
    category: str
    complexity: int
    prompt: str
    query: str
    subject: str | None = None
    depth: str = "medium"
    effort: int = 2
    allow_cross_domain: bool = False
    min_results: int = 1
    required_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    expected_source_kinds: tuple[str, ...] = ()
    expected_primary_domain: str | None = None
    expected_domain_relations: tuple[str, ...] = ()
    expected_filters: tuple[tuple[str, Any], ...] = ()
    expected_governance_labels: tuple[str, ...] = ()
    require_score_breakdown: bool = False
    require_validation_checks: bool = False


def memory_eval_conversations() -> tuple[MemoryEvalConversation, ...]:
    return (
        MemoryEvalConversation(
            id="eval-lab",
            title="Lab automation parser",
            subject="Lab Automation",
            messages=(
                MemoryEvalMessage(
                    id="lab-u1",
                    role="user",
                    text=(
                        "I decided we will use the barcode parser for plate reader CSV imports. "
                        "The parser maps sample barcodes to assay plate positions before analysis."
                    ),
                ),
                MemoryEvalMessage(
                    id="lab-a1",
                    role="assistant",
                    text="Use the lab automation barcode parser workflow and add a dry-run before writing outputs.",
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-secret",
            title="Credential note",
            messages=(
                MemoryEvalMessage(
                    id="secret-u1",
                    role="user",
                    text="credential sk-abcdefghijklmnopqrstuvwxyz123456 should be redacted from any memory snippet",
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-recipe",
            title="Miso butter recipe card",
            subject="Recipes and Baking",
            messages=(
                MemoryEvalMessage(
                    id="recipe-u1",
                    role="user",
                    text=(
                        "For recipes I prefer concise AI-readable cards. Miso butter ingredients are "
                        "white miso, unsalted butter, lemon zest, and black pepper. Steps are soften "
                        "butter, mix everything, shape, chill, and serve on toast."
                    ),
                ),
                MemoryEvalMessage(
                    id="recipe-a1",
                    role="assistant",
                    text="I can turn that into a minimalist recipe card, but it should remain a draft until confirmed.",
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-memory",
            title="Memory system allowances",
            subject="Memory System",
            messages=(
                MemoryEvalMessage(
                    id="memory-u1",
                    role="user",
                    text=(
                        "I need domain detection with model assistance, but assistant suggestions should "
                        "always stay separate until confirmation in the first release."
                    ),
                ),
                MemoryEvalMessage(
                    id="memory-a1",
                    role="assistant",
                    text="The memory system should expose traces for retrieval, review, promotion, and failures.",
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-home-mcp",
            title="Home MCP memory bridge",
            subject="Home MCP",
            messages=(
                MemoryEvalMessage(
                    id="mcp-u1",
                    role="user",
                    text=(
                        "The Home MCP project should expose memory_status, memory_search, recipe tools, "
                        "and trace run IDs through narrow JSON-RPC tools."
                    ),
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-health",
            title="Sleep supplement caution",
            subject="Health Notes",
            messages=(
                MemoryEvalMessage(
                    id="health-u1",
                    role="user",
                    text=(
                        "For health supplements I want cautious, source-backed notes. Melatonin dose ideas "
                        "should stay unverified unless current evidence is checked."
                    ),
                ),
                MemoryEvalMessage(
                    id="health-a1",
                    role="assistant",
                    text="A supplement dose suggestion should be treated as assistant-only until the user confirms it.",
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-relationship",
            title="Relationship context boundary",
            subject="Relationship Notes",
            messages=(
                MemoryEvalMessage(
                    id="relationship-u1",
                    role="user",
                    text=(
                        "When discussing partner or family conflict, keep notes contextual and avoid treating "
                        "one vent as a durable relationship fact."
                    ),
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-legal",
            title="Legal study caution",
            subject="Legal Study",
            messages=(
                MemoryEvalMessage(
                    id="legal-u1",
                    role="user",
                    text=(
                        "For LSAT and legal case briefs, cite source authority and do not turn old legal notes "
                        "into current legal advice."
                    ),
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-projects",
            title="Project catalog",
            subject="Project Catalog",
            messages=(
                MemoryEvalMessage(
                    id="projects-u1",
                    role="user",
                    text=(
                        "Active build candidates include the recipe book project, the memory system project, "
                        "and the Home MCP project."
                    ),
                ),
            ),
        ),
        MemoryEvalConversation(
            id="eval-open-loop",
            title="Baking camera open loop",
            subject="Baking Cameras",
            messages=(
                MemoryEvalMessage(
                    id="open-loop-u1",
                    role="user",
                    text="TODO later: decide whether ESP32 cameras belong in v1 or later for baking captures.",
                ),
            ),
        ),
    )


def memory_usage_prompt_cases() -> tuple[UsagePromptCase, ...]:
    return (
        UsagePromptCase(
            id="exact_lab_lookup",
            category="retrieval",
            complexity=1,
            prompt="Find the lab note about the barcode parser.",
            query="barcode parser",
            subject="Lab Automation",
            depth="full",
            required_terms=("lab automation parser",),
            expected_primary_domain="lab_automation",
            expected_filters=(("subject", "Lab Automation"),),
            require_score_breakdown=True,
        ),
        UsagePromptCase(
            id="natural_recipe_lookup",
            category="retrieval",
            complexity=2,
            prompt="What should an agent remember about my baking recipe format and miso butter?",
            query="what should an agent remember about my baking recipe format and miso butter",
            subject="Recipes and Baking",
            depth="full",
            required_terms=("miso butter",),
            expected_primary_domain="cooking_baking",
            expected_filters=(("subject", "Recipes and Baking"), ("fts_strategy", "broad_any_terms")),
            require_score_breakdown=True,
        ),
        UsagePromptCase(
            id="assistant_suggestion_policy",
            category="governance",
            complexity=2,
            prompt="Before promoting memories, check whether assistant suggestions are allowed by default.",
            query="assistant suggestions memory separate confirmation first release",
            subject="Memory System",
            depth="full",
            required_terms=("memory system allowances",),
            expected_primary_domain="ai_memory_systems",
            expected_filters=(("subject", "Memory System"),),
        ),
        UsagePromptCase(
            id="curated_mcp_lookup",
            category="curated-memory",
            complexity=3,
            prompt="Use curated memory to explain what Home MCP should expose.",
            query="Home MCP memory_status JSON-RPC trace run IDs",
            subject="Home MCP",
            depth="full",
            required_terms=("home mcp memory",),
            expected_source_kinds=("curated_memory",),
            expected_primary_domain="ai_memory_systems",
        ),
        UsagePromptCase(
            id="high_risk_finance",
            category="high-risk",
            complexity=3,
            prompt="Find only safe, current portfolio notes and show high-risk caution metadata.",
            query="portfolio rebalancing notes",
            depth="full",
            effort=4,
            required_terms=("portfolio", "rebalance"),
            forbidden_terms=("old portfolio note",),
            expected_source_kinds=("curated_memory",),
            expected_primary_domain="finance_investing",
            expected_governance_labels=("financial_caution",),
            require_validation_checks=True,
        ),
        UsagePromptCase(
            id="high_risk_health",
            category="high-risk",
            complexity=3,
            prompt="Find cautious health supplement memory without exposing assistant-only dosing advice as fact.",
            query="melatonin supplement dose health evidence",
            subject="Health Notes",
            depth="full",
            effort=4,
            required_terms=("sleep supplement caution",),
            expected_primary_domain="health_supplements",
            expected_governance_labels=("health_caution",),
            require_validation_checks=True,
        ),
        UsagePromptCase(
            id="high_risk_relationship",
            category="high-risk",
            complexity=3,
            prompt="Find relationship notes with context warnings rather than durable overclaims.",
            query="partner family relationship conflict context",
            subject="Relationship Notes",
            depth="full",
            effort=4,
            required_terms=("relationship context boundary",),
            expected_primary_domain="relationships_life",
            expected_governance_labels=("relationship_context",),
            require_validation_checks=True,
        ),
        UsagePromptCase(
            id="high_risk_legal",
            category="high-risk",
            complexity=3,
            prompt="Find legal-study memory with source-authority caution.",
            query="LSAT legal case brief source authority",
            subject="Legal Study",
            depth="full",
            effort=4,
            required_terms=("legal study caution",),
            expected_primary_domain="law_lsat",
            expected_governance_labels=("source_authority",),
            require_validation_checks=True,
        ),
        UsagePromptCase(
            id="project_catalog_lookup",
            category="project-catalog",
            complexity=2,
            prompt="Catalog active projects from memory without needing the real private export.",
            query="active build candidates memory system Home MCP project catalog",
            subject="Project Catalog",
            depth="full",
            required_terms=("project catalog",),
            expected_primary_domain="ai_memory_systems",
            expected_filters=(("subject", "Project Catalog"),),
        ),
        UsagePromptCase(
            id="open_loop_lookup",
            category="lifecycle",
            complexity=2,
            prompt="Find unresolved future work about baking cameras.",
            query="TODO later ESP32 cameras baking captures",
            subject="Baking Cameras",
            depth="full",
            required_terms=("baking camera open loop",),
            expected_primary_domain="cooking_baking",
            expected_filters=(("subject", "Baking Cameras"),),
        ),
        UsagePromptCase(
            id="source_conflict_lookup",
            category="source-conflict",
            complexity=4,
            prompt="Retrieve conflicting sourdough cadence notes so a later layer can reconcile them.",
            query="sourdough feeding cadence 12 hour 24 hour conflict",
            subject="Recipes and Baking",
            depth="full",
            effort=4,
            required_terms=("sourdough 12 hour cadence", "sourdough 24 hour cadence"),
            expected_primary_domain="cooking_baking",
            expected_source_kinds=("curated_memory",),
        ),
        UsagePromptCase(
            id="cross_domain_transfer",
            category="cross-domain",
            complexity=4,
            prompt="Find an adjacent-domain lab checklist that may transfer to a baking workflow.",
            query="baking checklist",
            depth="full",
            effort=4,
            required_terms=("transferable lab checklist",),
            expected_source_kinds=("curated_memory",),
            expected_primary_domain="cooking_baking",
            expected_domain_relations=("transfer",),
        ),
        UsagePromptCase(
            id="blocked_source_suppression",
            category="governance",
            complexity=4,
            prompt="Search for recipe spam and verify tombstoned memory is suppressed.",
            query="blocked recipe spam source",
            depth="full",
            effort=4,
            min_results=0,
            forbidden_terms=("blocked recipe spam source",),
            expected_primary_domain="cooking_baking",
        ),
        UsagePromptCase(
            id="holistic_recipe_agent_context",
            category="holistic",
            complexity=4,
            prompt=(
                "Act like the memory layer for a recipe assistant: retrieve the relevant recipe facts, "
                "respect confirmation boundaries, and provide traceable source-backed context."
            ),
            query="recipe assistant miso butter confirmed recipe facts assistant draft separate",
            subject="Recipes and Baking",
            depth="full",
            effort=4,
            required_terms=("miso butter", "recipe card"),
            expected_primary_domain="cooking_baking",
            expected_filters=(("subject", "Recipes and Baking"),),
            require_score_breakdown=True,
            require_validation_checks=True,
        ),
    )


def fixture_status() -> str:
    return "ready"
