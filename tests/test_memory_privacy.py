from local_agent_lab.memory.privacy import (
    SourceExposure,
    detect_obvious_secrets,
    filter_allowed_sources,
    is_tracking_param,
    redact_obvious_secrets,
    should_expose_source,
    strip_tracking_params,
    summarize_source_exposure,
)


def test_strip_tracking_params_preserves_useful_query_and_fragment() -> None:
    url = (
        "https://example.com/article?utm_source=newsletter&id=42&fbclid=abc"
        "&q=plate+reader#section"
    )

    cleaned = strip_tracking_params(url)

    assert cleaned == "https://example.com/article?id=42&q=plate+reader#section"


def test_strip_tracking_params_supports_extra_params() -> None:
    url = "https://example.com/?sessionid=abc&keep=yes"

    assert strip_tracking_params(url, extra_tracking_params={"sessionid"}) == "https://example.com/?keep=yes"


def test_is_tracking_param_handles_prefixes_and_known_names() -> None:
    assert is_tracking_param("utm_medium")
    assert is_tracking_param("GCLID")
    assert not is_tracking_param("query")


def test_detect_and_redact_obvious_secrets() -> None:
    text = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 "
        "github=ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "password = hunter2"
    )

    findings = detect_obvious_secrets(text)
    redacted = redact_obvious_secrets(text)

    assert [finding.kind for finding in findings] == [
        "openai_api_key",
        "github_token",
        "assigned_secret",
    ]
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in redacted.text
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in redacted.text
    assert "hunter2" not in redacted.text
    assert redacted.redacted_count == 3
    assert "[REDACTED:openai_api_key:" in redacted.text


def test_redaction_is_stable_without_exposing_secret_value() -> None:
    text = "token=secret-value-123"

    first = redact_obvious_secrets(text)
    second = redact_obvious_secrets(text)

    assert first.text == second.text
    assert "secret-value-123" not in first.text
    assert first.findings[0].fingerprint == second.findings[0].fingerprint


def test_source_exposure_summary_counts_sources_tiers_and_redactions() -> None:
    exposures = [
        SourceExposure(
            run_id="run_1",
            source_type="chatgpt_chunk",
            source_id="chunk_a",
            subject="lab automation",
            disclosure_tier="summary",
            fields_exposed=("title", "summary"),
            chars_exposed=120,
            redacted_secret_count=1,
        ),
        SourceExposure(
            run_id="run_1",
            source_type="curated_memory",
            source_id="mem_b",
            subject="lab automation",
            disclosure_tier="full_chunk",
            fields_exposed=("body",),
            chars_exposed=300,
            redacted_secret_count=0,
        ),
    ]

    summary = summarize_source_exposure(exposures)

    assert summary.total_sources == 2
    assert summary.total_chars_exposed == 420
    assert summary.total_redacted_secrets == 1
    assert summary.by_source_type == {"chatgpt_chunk": 1, "curated_memory": 1}
    assert summary.by_disclosure_tier == {"summary": 1, "full_chunk": 1}


def test_should_expose_source_blocks_tombstones_before_blocklists() -> None:
    decision = should_expose_source(
        source_id="conv_1",
        subject="Lab Automation",
        tombstoned_source_ids={"conv_1"},
        blocked_source_ids={"conv_1"},
        blocked_subjects={"lab automation"},
    )

    assert not decision.allowed
    assert decision.reason == "source_tombstoned"


def test_should_expose_source_blocks_subjects_case_insensitively() -> None:
    decision = should_expose_source(
        source_id="conv_2",
        subject="  Lab   Automation ",
        blocked_subjects={"lab automation"},
    )

    assert not decision.allowed
    assert decision.reason == "subject_blocked"


def test_filter_allowed_sources_returns_allowed_ids_and_denied_reasons() -> None:
    allowed, denied = filter_allowed_sources(
        ["a", "b", "c"],
        subjects_by_source_id={"a": "coding", "b": "finance", "c": "lab"},
        blocked_source_ids={"c"},
        blocked_subjects={"finance"},
    )

    assert allowed == ["a"]
    assert [decision.source_id for decision in denied] == ["b", "c"]
    assert [decision.reason for decision in denied] == ["subject_blocked", "source_blocked"]
