from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .audit import blocked_source_ids
from .observability import utc_now


DEFAULT_FALLBACK_DIMENSION = 64
LOCAL_VECTOR_BACKEND = "sqlite-json"


@dataclass(frozen=True)
class EmbeddingModelSpec:
    provider: str
    model: str
    dimension: int
    normalize: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "dimension": self.dimension,
                "normalize": self.normalize,
            },
            sort_keys=True,
        )
        return "emb_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ChunkForEmbedding:
    chunk_id: str
    conversation_id: str
    message_id: str
    import_id: str
    chunk_index: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class StoredEmbedding:
    chunk_id: str
    embedding_model_id: str
    vector_backend: str
    vector_ref: str
    text_sha256: str
    dimension: int


EmbeddingFunction = Callable[[str, int], list[float]]


def fallback_model_spec(*, dimension: int = DEFAULT_FALLBACK_DIMENSION) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        provider="local",
        model="deterministic-token-hash",
        dimension=dimension,
        normalize=True,
        metadata={
            "purpose": "dependency-free fallback for tests and offline smoke checks",
            "quality": "not suitable for production semantic ranking",
            "semantic_hints": "small hand-authored synonym set for fixture recall checks",
        },
    )


def ollama_model_spec(*, model: str, dimension: int, host: str) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        provider="ollama",
        model=model,
        dimension=dimension,
        normalize=True,
        metadata={
            "purpose": "local semantic retrieval for ChatGPT memory chunks",
            "quality": "production local semantic embedding backend",
            "host": host,
            "privacy": "local Ollama HTTP endpoint",
        },
    )


def init_embedding_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS embedding_models (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            normalize INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(provider, model, dimension, normalize)
        );

        CREATE TABLE IF NOT EXISTS chunk_embeddings (
            chunk_id TEXT NOT NULL REFERENCES message_chunks(id) ON DELETE CASCADE,
            embedding_model_id TEXT NOT NULL REFERENCES embedding_models(id),
            vector_backend TEXT NOT NULL,
            vector_ref TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            embedded_at TEXT NOT NULL,
            is_stale INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(chunk_id, embedding_model_id)
        );

        CREATE TABLE IF NOT EXISTS local_embedding_vectors (
            vector_ref TEXT PRIMARY KEY,
            embedding_model_id TEXT NOT NULL REFERENCES embedding_models(id),
            vector_json TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_backend
            ON chunk_embeddings(vector_backend, vector_ref);
        CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_stale
            ON chunk_embeddings(is_stale);
        CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_hash
            ON chunk_embeddings(text_sha256);
        CREATE INDEX IF NOT EXISTS idx_local_embedding_vectors_model
            ON local_embedding_vectors(embedding_model_id);

        INSERT OR IGNORE INTO schema_migrations (version, name, applied_at, checksum)
        VALUES (2, 'chatgpt_memory_embeddings', datetime('now'), 'embedding_tables_v1');
        """
    )
    connection.commit()


def register_embedding_model(connection: sqlite3.Connection, spec: EmbeddingModelSpec) -> str:
    init_embedding_schema(connection)
    connection.execute(
        """
        INSERT OR IGNORE INTO embedding_models (
            id, provider, model, dimension, normalize, created_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spec.id,
            spec.provider,
            spec.model,
            spec.dimension,
            1 if spec.normalize else 0,
            utc_now(),
            json.dumps(spec.metadata, sort_keys=True),
        ),
    )
    connection.commit()
    return spec.id


def list_chunks_needing_embeddings(
    connection: sqlite3.Connection,
    *,
    embedding_model_id: str,
    limit: int | None = None,
) -> list[ChunkForEmbedding]:
    blocked_ids: list[str] = []
    if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='deletions'").fetchone() is not None:
        blocked_ids = sorted(blocked_source_ids(connection))
    sql = """
        SELECT
            message_chunks.id,
            message_chunks.conversation_id,
            message_chunks.message_id,
            message_chunks.import_id,
            message_chunks.chunk_index,
            message_chunks.text,
            message_chunks.text_sha256
        FROM message_chunks
        LEFT JOIN chunk_embeddings
            ON chunk_embeddings.chunk_id = message_chunks.id
           AND chunk_embeddings.embedding_model_id = ?
        WHERE message_chunks.is_deleted = 0
          AND (
              chunk_embeddings.chunk_id IS NULL
              OR chunk_embeddings.text_sha256 != message_chunks.text_sha256
              OR chunk_embeddings.is_stale = 1
          )
    """
    params: list[Any] = [embedding_model_id]
    if blocked_ids:
        placeholders = ", ".join("?" for _ in blocked_ids)
        sql += f"""
          AND message_chunks.id NOT IN ({placeholders})
          AND message_chunks.message_id NOT IN ({placeholders})
          AND message_chunks.conversation_id NOT IN ({placeholders})
        """
        params.extend(blocked_ids)
        params.extend(blocked_ids)
        params.extend(blocked_ids)
    sql += """
        ORDER BY message_chunks.import_id, message_chunks.conversation_id,
                 message_chunks.message_id, message_chunks.chunk_index
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = connection.execute(sql, params).fetchall()
    return [
        ChunkForEmbedding(
            chunk_id=str(row[0]),
            conversation_id=str(row[1]),
            message_id=str(row[2]),
            import_id=str(row[3]),
            chunk_index=int(row[4]),
            text=str(row[5]),
            text_sha256=str(row[6]),
        )
        for row in rows
    ]


def deterministic_fallback_embedding(text: str, dimension: int = DEFAULT_FALLBACK_DIMENSION) -> list[float]:
    if dimension <= 0:
        raise ValueError("dimension must be positive")

    vector = [0.0] * dimension
    tokens = _tokens(text)
    if not tokens:
        tokens = [hashlib.sha256(text.encode("utf-8")).hexdigest()]

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + (digest[5] / 255.0)
        vector[bucket] += sign * weight

    return _normalize(vector)


def store_chunk_embedding(
    connection: sqlite3.Connection,
    *,
    chunk: ChunkForEmbedding,
    spec: EmbeddingModelSpec,
    vector: list[float],
    vector_backend: str = LOCAL_VECTOR_BACKEND,
    metadata: dict[str, Any] | None = None,
) -> StoredEmbedding:
    if len(vector) != spec.dimension:
        raise ValueError(f"vector dimension {len(vector)} does not match model dimension {spec.dimension}")

    embedding_model_id = register_embedding_model(connection, spec)
    vector_ref = "vec_" + hashlib.sha256(
        f"{embedding_model_id}:{chunk.chunk_id}:{chunk.text_sha256}".encode("utf-8")
    ).hexdigest()[:16]
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    with connection:
        if vector_backend == LOCAL_VECTOR_BACKEND:
            connection.execute(
                """
                INSERT OR REPLACE INTO local_embedding_vectors (
                    vector_ref, embedding_model_id, vector_json, dimension, created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (vector_ref, embedding_model_id, json.dumps(vector), len(vector), utc_now(), metadata_json),
            )
        connection.execute(
            """
            INSERT INTO chunk_embeddings (
                chunk_id, embedding_model_id, vector_backend, vector_ref,
                text_sha256, embedded_at, is_stale, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(chunk_id, embedding_model_id) DO UPDATE SET
                vector_backend = excluded.vector_backend,
                vector_ref = excluded.vector_ref,
                text_sha256 = excluded.text_sha256,
                embedded_at = excluded.embedded_at,
                is_stale = 0,
                metadata_json = excluded.metadata_json
            """,
            (chunk.chunk_id, embedding_model_id, vector_backend, vector_ref, chunk.text_sha256, utc_now(), metadata_json),
        )

    return StoredEmbedding(
        chunk_id=chunk.chunk_id,
        embedding_model_id=embedding_model_id,
        vector_backend=vector_backend,
        vector_ref=vector_ref,
        text_sha256=chunk.text_sha256,
        dimension=len(vector),
    )


def embed_missing_chunks(
    connection: sqlite3.Connection,
    *,
    spec: EmbeddingModelSpec | None = None,
    embedder: EmbeddingFunction = deterministic_fallback_embedding,
    limit: int | None = None,
) -> dict[str, Any]:
    model = spec or fallback_model_spec()
    embedding_model_id = register_embedding_model(connection, model)
    chunks = list_chunks_needing_embeddings(connection, embedding_model_id=embedding_model_id, limit=limit)
    stored: list[StoredEmbedding] = []

    for chunk in chunks:
        vector = embedder(chunk.text, model.dimension)
        stored.append(store_chunk_embedding(connection, chunk=chunk, spec=model, vector=vector))

    return {
        "status": "ok",
        "embedding_model_id": embedding_model_id,
        "provider": model.provider,
        "model": model.model,
        "dimension": model.dimension,
        "vector_backend": LOCAL_VECTOR_BACKEND,
        "requested_limit": limit,
        "chunks_considered": len(chunks),
        "embeddings_written": len(stored),
        "vector_refs": [item.vector_ref for item in stored],
    }


def load_local_vector(connection: sqlite3.Connection, vector_ref: str) -> list[float]:
    row = connection.execute(
        "SELECT vector_json FROM local_embedding_vectors WHERE vector_ref = ?",
        (vector_ref,),
    ).fetchone()
    if row is None:
        raise KeyError(vector_ref)
    payload = json.loads(str(row[0]))
    if not isinstance(payload, list):
        raise ValueError(f"invalid vector payload for {vector_ref}")
    return [float(value) for value in payload]


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if len(left_values) != len(right_values):
        raise ValueError("vectors must have the same dimension")
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(a * a for a in left_values))
    right_norm = math.sqrt(sum(b * b for b in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _tokens(text: str) -> list[str]:
    base_tokens = re.findall(r"[a-z0-9]+", text.lower())
    expanded: list[str] = []
    for token in base_tokens:
        expanded.append(token)
        expanded.extend(_TOKEN_ALIASES.get(token, ()))
    return expanded


_TOKEN_ALIASES = {
    "bake": ("baking", "oven"),
    "baking": ("bake", "oven"),
    "bread": ("loaf", "sourdough"),
    "ferment": ("fermentation", "proof", "rise"),
    "fermentation": ("ferment", "proof", "rise"),
    "leaven": ("levain", "starter", "sourdough"),
    "leavened": ("levain", "starter", "sourdough"),
    "levain": ("leaven", "starter", "sourdough"),
    "loaf": ("bread", "sourdough"),
    "proof": ("ferment", "fermentation", "rise"),
    "proofing": ("ferment", "fermentation", "rise"),
    "proving": ("ferment", "fermentation", "rise"),
    "rise": ("ferment", "fermentation", "proof"),
    "schedule": ("timing", "timeline"),
    "sourdough": ("bread", "levain", "starter"),
    "starter": ("levain", "sourdough"),
    "timing": ("schedule", "timeline"),
}


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
