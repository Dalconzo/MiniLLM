from __future__ import annotations

from .ontology import validate_domain


DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cooking_baking": ("recipe", "bake", "baking", "cook", "oven", "cake", "dough", "dessert", "sourdough"),
    "lab_automation": ("plate reader", "parser", "csv", "workflow", "pipette", "assay", "robot", "automation", "lab"),
    "career_work": ("job", "interview", "resume", "manager", "career", "work", "promotion", "workflow"),
    "ai_memory_systems": ("memory", "rag", "embedding", "retrieval", "llm", "ollama", "vector", "subject"),
    "finance_investing": ("portfolio", "stock", "market", "invest", "trade", "401k", "ira", "rebalancing", "funds"),
    "health_supplements": ("supplement", "vitamin", "health", "sleep", "dose", "wellness"),
    "relationships_life": ("partner", "friend", "family", "relationship", "dating"),
    "law_lsat": ("lsat", "brief", "case", "legal", "law"),
    "style_wardrobe": ("outfit", "wardrobe", "style", "shirt", "jacket"),
    "creative_writing": ("story", "character", "plot", "novel", "poem", "write"),
    "home_projects": ("home", "garage", "shelf", "repair", "wood", "tool"),
    "fitness_training": ("workout", "run", "training", "lift", "gym"),
    "pets": ("dog", "cat", "pet", "vet"),
}

DOMAIN_ADJACENCY: dict[str, tuple[str, ...]] = {
    "cooking_baking": ("fitness_training", "health_supplements", "relationships_life", "lab_automation", "ai_memory_systems"),
    "lab_automation": ("career_work", "ai_memory_systems", "cooking_baking", "home_projects"),
    "finance_investing": ("career_work", "ai_memory_systems"),
    "health_supplements": ("fitness_training", "cooking_baking"),
    "relationships_life": ("cooking_baking", "career_work", "style_wardrobe"),
}

DEFAULT_LENSES_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "cooking_baking": ("procedural", "planning", "preference"),
    "lab_automation": ("operational", "procedural", "planning"),
    "career_work": ("planning", "identity_pattern", "operational"),
    "ai_memory_systems": ("operational", "planning", "source_authority"),
    "finance_investing": ("financial_caution", "planning", "source_authority"),
    "health_supplements": ("health_caution", "temporal", "source_authority"),
    "relationships_life": ("relationship_context", "temporal", "identity_pattern"),
    "law_lsat": ("source_authority", "planning", "contradiction"),
    "style_wardrobe": ("preference", "identity_pattern", "planning"),
    "creative_writing": ("creativity", "analogy", "planning"),
    "home_projects": ("operational", "planning", "workaround"),
    "fitness_training": ("procedural", "temporal", "planning"),
    "pets": ("operational", "temporal", "planning"),
    "misc": ("operational", "planning"),
}

HIGH_RISK_DOMAINS = ("health_supplements", "finance_investing", "relationships_life", "law_lsat")
HIGH_RISK_LENSES = {
    "health_supplements": "health_caution",
    "finance_investing": "financial_caution",
    "relationships_life": "relationship_context",
    "law_lsat": "source_authority",
}


def detect_query_domains(query: str) -> list[str]:
    detected = _detect_domains(query)
    return detected or ["misc"]


def classify_text_domains(*parts: str | None) -> list[str]:
    text = " ".join(part for part in parts if part).lower()
    detected = _detect_domains(text)
    return detected or ["misc"]


def scope_candidate_domains(
    query_domains: list[str],
    candidate_domains: list[str],
    *,
    effort: int = 2,
    allow_cross_domain: bool = False,
) -> tuple[bool, str]:
    normalized_query = [domain for domain in query_domains if domain != "misc"] or ["misc"]
    normalized_candidate = candidate_domains or ["misc"]

    if "misc" in normalized_query:
        return True, "broad"

    if set(normalized_query) & set(normalized_candidate):
        return True, "primary"

    if allow_cross_domain:
        return True, "analogy"

    if effort >= 3:
        if _is_adjacent(normalized_query, normalized_candidate):
            return True, "transfer"
        if effort >= 4:
            return True, "analogy"

    return False, "excluded"


def dominant_domain(domains: list[str]) -> str:
    for domain in domains:
        if domain != "misc":
            return validate_domain(domain)
    return "misc"


def select_lenses_for_query(query_domains: list[str], effort: int) -> list[str]:
    selected: list[str] = []
    for domain in query_domains or ["misc"]:
        for lens in DEFAULT_LENSES_BY_DOMAIN.get(domain, DEFAULT_LENSES_BY_DOMAIN["misc"]):
            if lens not in selected:
                selected.append(lens)

    if effort >= 4:
        for lens in ("temporal", "contradiction"):
            if lens not in selected:
                selected.append(lens)
    if effort >= 5:
        for lens in ("analogy", "creativity"):
            if lens not in selected:
                selected.append(lens)
    return selected


def high_risk_domains(query_domains: list[str]) -> list[str]:
    return [domain for domain in query_domains if domain in HIGH_RISK_DOMAINS]


def high_risk_lenses(query_domains: list[str]) -> list[str]:
    lenses: list[str] = []
    for domain in high_risk_domains(query_domains):
        lens = HIGH_RISK_LENSES[domain]
        if lens not in lenses:
            lenses.append(lens)
    return lenses


def apply_governance_policy(
    query_domains: list[str],
    candidate_domains: list[str],
    *,
    effort: int,
    allow_cross_domain: bool,
    candidate_status: str | None = None,
    candidate_trust_level: str | None = None,
    source_role: str | None = None,
    domain_relation: str = "primary",
) -> tuple[bool, str, list[str]]:
    query_risk = high_risk_domains(query_domains)
    if not query_risk:
        return True, "none", []

    labels = high_risk_lenses(query_domains)
    candidate_risk = [domain for domain in candidate_domains if domain in HIGH_RISK_DOMAINS]
    if domain_relation != "primary" and not allow_cross_domain:
        return False, "cross_domain_blocked", labels
    if candidate_status in {"stale", "superseded", "archived", "deleted"}:
        return False, "stale_high_risk_memory", labels
    if candidate_trust_level and candidate_trust_level not in {"high", "canonical"}:
        return False, "low_trust_high_risk_memory", labels
    if source_role == "assistant":
        return False, "assistant_only_suggestion", labels
    if effort < 4 and candidate_risk and domain_relation != "primary":
        return False, "high_risk_cross_domain_blocked", labels
    return True, "high_risk_allowed", labels


def _detect_domains(text: str) -> list[str]:
    lowered = text.lower()
    detected: list[str] = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            detected.append(validate_domain(domain))
    return detected


def _is_adjacent(query_domains: list[str], candidate_domains: list[str]) -> bool:
    candidate_set = set(candidate_domains)
    for query_domain in query_domains:
        adjacent = set(DOMAIN_ADJACENCY.get(query_domain, ()))
        if adjacent & candidate_set:
            return True
    return False
