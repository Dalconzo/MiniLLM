from local_agent_lab.memory.privacy import (
    SourceExposure,
    detect_obvious_secrets,
    filter_allowed_sources,
    is_tracking_param,
    redact_obvious_secrets,
    redact_secrets_in_payload_for_storage,
    redact_secrets_for_storage,
    should_expose_source,
    strip_tracking_params,
    summarize_source_exposure,
)


def test_strip_tracking_params_preserves_useful_query_and_fragment() -> None:
    url = "https://example.com/article?utm_source=newsletter&id=42&fbclid=abc&q=plate+reader#section"

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
    openai_key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    text = f"OPENAI_API_KEY={openai_key} github={github_token} password = hunter2"

    findings = detect_obvious_secrets(text)
    redacted = redact_obvious_secrets(text)

    assert [finding.kind for finding in findings] == [
        "openai_api_key",
        "github_token",
        "assigned_secret",
    ]
    assert openai_key not in redacted.text
    assert github_token not in redacted.text
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


def test_storage_redaction_does_not_retain_value_fingerprint() -> None:
    result = redact_secrets_for_storage("token=secret-value-123")

    assert result.text == "token=[REDACTED:assigned_secret]"
    assert "secret-value-123" not in result.text
    assert result.findings[0].fingerprint not in result.text


def test_storage_redaction_handles_quoted_structured_credentials() -> None:
    bearer_secret = "signed-" + "secret-value"
    source = f'{{"password": "correct horse battery staple", "authorization": "Bearer {bearer_secret}"}}'

    result = redact_secrets_for_storage(source)

    assert "correct horse battery staple" not in result.text
    assert "signed-secret-value" not in result.text
    assert result.redacted_count == 2


def test_storage_redaction_handles_multiline_auth_url_and_multiword_assignments() -> None:
    credential_url = "https://private-user:" + "private-pass@example.test/path"
    source = "\n".join(
        [
            "Authorization: Bearer signed secret with spaces",
            "password=correct horse battery staple",
            credential_url,
        ]
    )

    result = redact_secrets_for_storage(source)

    assert "signed secret with spaces" not in result.text
    assert "correct horse battery staple" not in result.text
    assert "private-user:private-pass" not in result.text
    assert {finding.kind for finding in result.findings} == {
        "authorization_header",
        "assigned_secret",
        "url_userinfo",
    }


def test_payload_storage_redaction_recurses_through_nested_values() -> None:
    canary = "sk-" + "browseracceptance000000000000"
    payload = {
        "query": canary,
        "filters": [{"field": "title", "value": canary}],
        "nested": ("safe", {"message": canary}),
    }

    redacted = redact_secrets_in_payload_for_storage(payload)

    assert canary not in str(redacted)
    assert redacted["query"] == "[REDACTED:openai_api_key]"
    assert redacted["filters"][0]["value"] == "[REDACTED:openai_api_key]"
    assert redacted["nested"][1]["message"] == "[REDACTED:openai_api_key]"


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
