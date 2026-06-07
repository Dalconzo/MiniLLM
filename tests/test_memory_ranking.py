from datetime import datetime, timezone

import pytest

from local_agent_lab.memory.ranking import (
    EXPOSED_FIELDS_BY_TIER,
    RankingSignals,
    disclosure_tier_for_score,
    rank_memory_hits,
    score_memory_hit,
)


def _hits():
    return [
        {
            "conversation_id": "conv_a",
            "message_id": "msg_a",
            "chunk_id": "chk_a",
            "title": "Barcode parser",
            "score_breakdown": {"fts_bm25": -1.0},
            "message_created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "conversation_id": "conv_b",
            "message_id": "msg_b",
            "chunk_id": "chk_b",
            "title": "Generic notes",
            "score_breakdown": {"fts_bm25": -3.0},
            "message_created_at": "2025-01-01T00:00:00+00:00",
        },
        {
            "conversation_id": "conv_c",
            "message_id": "msg_c",
            "chunk_id": "chk_c",
            "title": "Lab automation memory",
            "score_breakdown": {"fts_bm25": -2.0},
            "message_created_at": "2024-01-01T00:00:00+00:00",
        },
    ]


def test_rank_memory_hits_combines_keyword_semantic_subject_trust_feedback_and_recency() -> None:
    ranked = rank_memory_hits(
        _hits(),
        signals=RankingSignals(
            semantic_similarity={"chk_a": 0.99, "chk_b": 0.10, "chk_c": 0.60},
            subject_match={"conv_a": 1.0, "conv_c": 0.5},
            curated_trust={"chk_a": 0.8, "chk_b": 0.1, "chk_c": 0.5},
            feedback={"conv_a": 1.0, "conv_b": 0.0, "conv_c": 0.4},
        ),
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert [hit["chunk_id"] for hit in ranked] == ["chk_a", "chk_c", "chk_b"]
    assert [hit["rank"] for hit in ranked] == [1, 2, 3]

    top = ranked[0]
    components = top["score_breakdown"]["components"]
    assert top["score"] == top["score_breakdown"]["final_score"]
    assert components["keyword_relevance"]["value"] == 0.0
    assert components["semantic_similarity"]["contribution"] == pytest.approx(0.2475)
    assert components["subject_match"]["contribution"] == pytest.approx(0.15)
    assert components["curated_trust"]["contribution"] == pytest.approx(0.08)
    assert components["feedback"]["contribution"] == pytest.approx(0.10)
    assert components["recency"]["value"] > ranked[1]["score_breakdown"]["components"]["recency"]["value"]
    assert top["ranking_profile"] == "hybrid_memory_v1"


def test_rank_memory_hits_normalizes_bm25_lower_is_better() -> None:
    ranked = rank_memory_hits(_hits(), now=datetime(2026, 6, 1, tzinfo=timezone.utc))

    by_chunk = {hit["chunk_id"]: hit for hit in ranked}
    assert by_chunk["chk_b"]["score_breakdown"]["components"]["keyword_relevance"]["value"] == 1.0
    assert by_chunk["chk_c"]["score_breakdown"]["components"]["keyword_relevance"]["value"] == 0.5
    assert by_chunk["chk_a"]["score_breakdown"]["components"]["keyword_relevance"]["value"] == 0.0


def test_explicit_keyword_relevance_overrides_bm25_normalization() -> None:
    ranked = rank_memory_hits(
        [
            {"chunk_id": "low", "keyword_relevance": 0.2, "score_breakdown": {"fts_bm25": -100.0}},
            {"chunk_id": "high", "keyword_relevance": 0.9, "score_breakdown": {"fts_bm25": -1.0}},
        ]
    )

    assert [hit["chunk_id"] for hit in ranked] == ["high", "low"]
    assert ranked[0]["score_breakdown"]["components"]["keyword_relevance"]["value"] == 0.9


def test_penalties_reduce_final_score_and_are_inspectable() -> None:
    ranked = rank_memory_hits(
        [{"chunk_id": "clean", "keyword_relevance": 1.0}, {"chunk_id": "spam", "keyword_relevance": 1.0}],
        signals=RankingSignals(spam_penalty={"spam": 1.0}, paywall_penalty={"spam": 1.0}),
    )

    assert [hit["chunk_id"] for hit in ranked] == ["clean", "spam"]
    spam = ranked[1]
    assert spam["score_breakdown"]["components"]["spam_penalty"]["contribution"] == -0.2
    assert spam["score_breakdown"]["components"]["paywall_penalty"]["contribution"] == -0.1
    assert spam["score"] == pytest.approx(0.05)


def test_disclosure_tiers_are_score_based_and_capped_by_depth() -> None:
    assert disclosure_tier_for_score(0.10, depth="full") == "far"
    assert disclosure_tier_for_score(0.50, depth="full") == "medium"
    assert disclosure_tier_for_score(0.70, depth="full") == "close"
    assert disclosure_tier_for_score(0.90, depth="full") == "full"
    assert disclosure_tier_for_score(0.90, depth="medium") == "medium"
    assert disclosure_tier_for_score(0.70, depth="far") == "far"


def test_rank_memory_hits_attaches_exposed_fields_for_tier() -> None:
    ranked = rank_memory_hits(
        [{"chunk_id": "chk", "keyword_relevance": 1.0}],
        signals=RankingSignals(semantic_similarity={"chk": 1.0}, subject_match={"chk": 1.0}, feedback={"chk": 1.0}),
        depth="close",
    )

    assert ranked[0]["disclosure_tier"] == "close"
    assert ranked[0]["exposed_fields"] == list(EXPOSED_FIELDS_BY_TIER["close"])


def test_stable_tie_breaking_uses_candidate_ids() -> None:
    ranked = rank_memory_hits(
        [
            {"conversation_id": "conv_b", "message_id": "msg_b", "chunk_id": "chk_b", "keyword_relevance": 0.5},
            {"conversation_id": "conv_a", "message_id": "msg_a", "chunk_id": "chk_a", "keyword_relevance": 0.5},
        ]
    )

    assert [hit["chunk_id"] for hit in ranked] == ["chk_a", "chk_b"]


def test_score_memory_hit_scores_one_candidate_with_normalized_inputs() -> None:
    hit = score_memory_hit(
        {"chunk_id": "chk"},
        keyword_relevance=1.0,
        semantic_similarity=1.0,
        subject_match=1.0,
        curated_trust=1.0,
        feedback=1.0,
        recency=1.0,
    )

    assert hit["score"] == 1.0
    assert hit["disclosure_tier"] == "full"


def test_invalid_depth_is_rejected() -> None:
    with pytest.raises(ValueError, match="depth must be one of"):
        rank_memory_hits([], depth="wide")
