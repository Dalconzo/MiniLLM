from local_agent_lab.indexing.chunker import chunk_text


def test_chunk_text_splits_long_input() -> None:
    chunks = chunk_text("a" * 2500, chunk_size=1000)
    assert len(chunks) == 3
    assert chunks[0] == "a" * 1000
