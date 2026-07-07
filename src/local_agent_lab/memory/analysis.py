from __future__ import annotations

import html
import re
import sqlite3
from pathlib import Path
from collections import Counter
from typing import Any

from .observability import list_recent_runs, memory_db_path, utc_now
from .subjects import list_subjects


def analyze_memory_corpus(
    *,
    data_dir: Path,
    memory_dir: Path,
    logs_dir: Path,
    subject_limit: int = 10,
    recent_limit: int = 5,
) -> dict[str, Any]:
    sqlite_path = memory_db_path(memory_dir)
    report: dict[str, Any] = {
        "status": "warn",
        "checked_at": utc_now(),
        "data_dir": str(data_dir),
        "memory_dir": str(memory_dir),
        "sqlite_path": str(sqlite_path),
        "counts": {},
        "token_stats": {},
        "role_stats": [],
        "candidate_stats": {},
        "codex": {},
        "subjects": [],
        "imports": [],
        "recent_runs": list_recent_runs(logs_dir, limit=recent_limit),
    }
    if not sqlite_path.exists():
        report["error"] = {
            "stage": "analyze_corpus",
            "error_code": "memory_database_not_found",
            "message": f"ChatGPT memory database does not exist: {sqlite_path}",
        }
        return report

    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        report["counts"] = _counts(connection)
        report["token_stats"] = _token_stats(connection)
        report["role_stats"] = _role_stats(connection)
        report["candidate_stats"] = _candidate_stats(connection, total_chunks=report["counts"].get("message_chunks", 0))
        report["codex"] = _codex_stats(connection)
        report["subjects"] = [subject.to_dict() for subject in list_subjects(connection, limit=subject_limit)]
        report["imports"] = _imports(connection)
        report["status"] = "ok"
    return report


def render_memory_analysis(report: dict[str, Any]) -> str:
    lines = [
        f"Status: {report['status']}",
        f"Checked: {report['checked_at']}",
        f"Data: {report['data_dir']}",
        f"Memory DB: {report['sqlite_path']}",
        "Corpus Shape:",
    ]
    counts = report.get("counts", {})
    lines.extend(
        [
            f"- imports={counts.get('imports', 0)} conversations={counts.get('conversations', 0)} messages={counts.get('messages', 0)} chunks={counts.get('message_chunks', 0)}",
            f"- candidates={counts.get('candidate_memories', 0)} curated={counts.get('memory_records', 0)} subjects={counts.get('subjects', 0)} embeddings={counts.get('chunk_embeddings', 0)}",
        ]
    )

    token_stats = report.get("token_stats", {})
    if token_stats:
        lines.extend(
            [
                "Token Stats:",
                f"- avg_chunk_tokens={_fmt_float(token_stats.get('avg_chunk_tokens'))}",
                f"- avg_user_chunk_tokens={_fmt_float(token_stats.get('avg_user_chunk_tokens'))}",
                f"- avg_assistant_chunk_tokens={_fmt_float(token_stats.get('avg_assistant_chunk_tokens'))}",
                f"- user_chunks={token_stats.get('user_chunks', 0)} assistant_chunks={token_stats.get('assistant_chunks', 0)}",
            ]
        )

    candidate_stats = report.get("candidate_stats", {})
    if candidate_stats:
        lines.extend(
            [
                "Candidate Memory:",
                f"- total={candidate_stats.get('total', 0)} user_candidates={candidate_stats.get('user_candidates', 0)} assistant_candidates={candidate_stats.get('assistant_candidates', 0)}",
                f"- user_fact_density_per_1000_chunks={_fmt_float(candidate_stats.get('per_1000_chunks'))}",
            ]
        )

    codex = report.get("codex", {})
    if codex:
        lines.extend(
            [
                "Codex Coverage:",
                f"- title_conversations={codex.get('title_conversations', 0)} message_hits={codex.get('message_hits', 0)} conversation_hits={codex.get('conversation_hits', 0)}",
                f"- chunk_hits={codex.get('chunk_hits', 0)}",
            ]
        )

    role_stats = report.get("role_stats", [])
    if role_stats:
        lines.append("Role Mix:")
        for row in role_stats:
            lines.append(
                f"- {row['role']}: chunks={row['chunks']} avg_tokens={_fmt_float(row['avg_tokens'])} total_tokens={row['total_tokens']}"
            )

    subjects = report.get("subjects", [])
    if subjects:
        lines.append("Top Subjects:")
        for subject in subjects:
            lines.append(
                f"- {subject['kind']}:{subject['slug']} {subject['name']} conversations={subject['conversation_count']} chunks={subject['chunk_count']}"
            )

    imports = report.get("imports", [])
    if imports:
        lines.append("Imports:")
        for item in imports:
            lines.append(
                f"- {item['id']} [{item['status']}] {item['conversation_count']} conversations / {item['message_count']} messages / {item['chunk_count']} chunks"
            )

    recent_runs = report.get("recent_runs", [])
    lines.append("Recent Runs:")
    if not recent_runs:
        lines.append("- none")
    else:
        for item in recent_runs:
            lines.append(
                f"- {item['run_id']} [{item.get('status')}] {item.get('command')} started={item.get('started_at')} finished={item.get('finished_at')}"
            )
    return "\n".join(lines)


def render_memory_analysis_html(report: dict[str, Any]) -> str:
    def section(title: str, body: str) -> str:
        return f"<section class='card'><h2>{html.escape(title)}</h2>{body}</section>"

    def table(headers: list[str], rows: list[list[str]]) -> str:
        head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    counts = report.get("counts", {})
    token_stats = report.get("token_stats", {})
    candidate_stats = report.get("candidate_stats", {})
    codex = report.get("codex", {})
    role_stats = report.get("role_stats", [])
    subjects = report.get("subjects", [])
    imports = report.get("imports", [])
    recent_runs = report.get("recent_runs", [])

    summary_cards = "".join(
        _card(label, value)
        for label, value in [
            ("Status", str(report.get("status", "unknown"))),
            ("Conversations", str(counts.get("conversations", 0))),
            ("Messages", str(counts.get("messages", 0))),
            ("Chunks", str(counts.get("message_chunks", 0))),
            ("Candidates", str(counts.get("candidate_memories", 0))),
            ("Subjects", str(counts.get("subjects", 0))),
        ]
    )

    role_rows = [
        [row["role"], str(row["chunks"]), _fmt_float(row["avg_tokens"]), str(row["total_tokens"])]
        for row in role_stats
    ]
    subject_rows = [
        [f"{subject['kind']}/{subject['slug']}", subject["name"], str(subject["conversation_count"]), str(subject["chunk_count"])]
        for subject in subjects
    ]
    import_rows = [
        [item["id"], item["status"], str(item["conversation_count"]), str(item["message_count"]), str(item["chunk_count"])]
        for item in imports
    ]
    recent_rows = [
        [item["run_id"], item.get("status") or "", item.get("command") or "", item.get("started_at") or "", item.get("finished_at") or ""]
        for item in recent_runs
    ]

    body = f"""
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1' />
        <title>Memory Corpus Analysis</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f5f2ea;
            --panel: #fffdf8;
            --text: #1d1a16;
            --muted: #6b6257;
            --accent: #7b4f2a;
            --border: #d7cdbf;
          }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(180deg, #f8f4ec 0%, #efe7da 100%);
            color: var(--text);
          }}
          .wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
          .hero {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: end; justify-content: space-between; margin-bottom: 20px; }}
          .hero h1 {{ margin: 0; font-size: 2rem; }}
          .hero p {{ margin: 6px 0 0; color: var(--muted); }}
          .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }}
          .metric {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 14px; box-shadow: 0 10px 20px rgba(68, 52, 35, 0.05); }}
          .metric .label {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
          .metric .value {{ font-size: 1.5rem; margin-top: 4px; font-weight: 650; }}
          .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 18px; margin-bottom: 16px; box-shadow: 0 12px 26px rgba(68, 52, 35, 0.05); }}
          h2 {{ margin: 0 0 12px; font-size: 1.1rem; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 0.95rem; }}
          th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
          th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; }}
          .muted {{ color: var(--muted); }}
          code {{ background: #f1e7d9; padding: 2px 6px; border-radius: 8px; }}
        </style>
      </head>
      <body>
        <div class='wrap'>
          <div class='hero'>
            <div>
              <h1>Memory Corpus Analysis</h1>
              <p>{html.escape(str(report.get("checked_at", "")))}</p>
              <p class='muted'>{html.escape(str(report.get("sqlite_path", "")))}</p>
            </div>
          </div>
          <div class='grid'>{summary_cards}</div>
          {section("Corpus Shape", table(["Metric", "Value"], [
              ["Imports", str(counts.get("imports", 0))],
              ["Conversations", str(counts.get("conversations", 0))],
              ["Messages", str(counts.get("messages", 0))],
              ["Chunks", str(counts.get("message_chunks", 0))],
              ["Candidates", str(counts.get("candidate_memories", 0))],
              ["Curated", str(counts.get("memory_records", 0))],
              ["Subjects", str(counts.get("subjects", 0))],
              ["Embeddings", str(counts.get("chunk_embeddings", 0))],
          ]))}
          {section("Token Stats", table(["Metric", "Value"], [
              ["Avg chunk tokens", _fmt_float(token_stats.get("avg_chunk_tokens"))],
              ["Avg user chunk tokens", _fmt_float(token_stats.get("avg_user_chunk_tokens"))],
              ["Avg assistant chunk tokens", _fmt_float(token_stats.get("avg_assistant_chunk_tokens"))],
              ["User chunks", str(token_stats.get("user_chunks", 0))],
              ["Assistant chunks", str(token_stats.get("assistant_chunks", 0))],
          ]))}
          {section("Candidate Memory", table(["Metric", "Value"], [
              ["Total", str(candidate_stats.get("total", 0))],
              ["User candidates", str(candidate_stats.get("user_candidates", 0))],
              ["Assistant candidates", str(candidate_stats.get("assistant_candidates", 0))],
              ["User fact density / 1000 chunks", _fmt_float(candidate_stats.get("per_1000_chunks"))],
          ]))}
          {section("Codex Coverage", table(["Metric", "Value"], [
              ["Title hits", str(codex.get("title_conversations", 0))],
              ["Message hits", str(codex.get("message_hits", 0))],
              ["Conversation hits", str(codex.get("conversation_hits", 0))],
              ["Chunk hits", str(codex.get("chunk_hits", 0))],
          ]))}
          {section("Role Mix", table(["Role", "Chunks", "Avg tokens", "Total tokens"], role_rows or [["-", "0", "0", "0"]]))}
          {section("Top Subjects", table(["Subject", "Name", "Conversations", "Chunks"], subject_rows or [["-", "-", "0", "0"]]))}
          {section("Imports", table(["Import", "Status", "Conversations", "Messages", "Chunks"], import_rows or [["-", "-", "0", "0", "0"]]))}
          {section("Recent Runs", table(["Run", "Status", "Command", "Started", "Finished"], recent_rows or [["-", "-", "-", "-", "-"]]))}
        </div>
      </body>
    </html>
    """
    return "\n".join(line.rstrip() for line in body.splitlines())


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "imports",
        "conversations",
        "messages",
        "message_chunks",
        "candidate_memories",
        "memory_records",
        "subjects",
        "chunk_embeddings",
    ):
        try:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            counts[table] = 0
    return counts


def _token_stats(connection: sqlite3.Connection) -> dict[str, Any]:
    stats = connection.execute(
        """
        SELECT
            COALESCE(ROUND(AVG(message_chunks.token_estimate), 2), 0),
            COALESCE(ROUND(AVG(CASE WHEN messages.role = 'user' THEN message_chunks.token_estimate END), 2), 0),
            COALESCE(ROUND(AVG(CASE WHEN messages.role = 'assistant' THEN message_chunks.token_estimate END), 2), 0),
            COALESCE(SUM(CASE WHEN messages.role = 'user' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN messages.role = 'assistant' THEN 1 ELSE 0 END), 0)
        FROM message_chunks
        JOIN messages ON messages.id = message_chunks.message_id
        WHERE message_chunks.is_deleted = 0 AND messages.is_deleted = 0
        """
    ).fetchone()
    return {
        "avg_chunk_tokens": float(stats[0] or 0),
        "avg_user_chunk_tokens": float(stats[1] or 0),
        "avg_assistant_chunk_tokens": float(stats[2] or 0),
        "user_chunks": int(stats[3] or 0),
        "assistant_chunks": int(stats[4] or 0),
    }


def _role_stats(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            messages.role,
            COUNT(*) AS chunk_count,
            ROUND(AVG(message_chunks.token_estimate), 2) AS avg_tokens,
            SUM(message_chunks.token_estimate) AS total_tokens
        FROM message_chunks
        JOIN messages ON messages.id = message_chunks.message_id
        WHERE message_chunks.is_deleted = 0 AND messages.is_deleted = 0
        GROUP BY messages.role
        ORDER BY chunk_count DESC, messages.role ASC
        """
    ).fetchall()
    return [
        {
            "role": row[0],
            "chunks": int(row[1] or 0),
            "avg_tokens": float(row[2] or 0),
            "total_tokens": int(row[3] or 0),
        }
        for row in rows
    ]


def _candidate_stats(connection: sqlite3.Connection, *, total_chunks: int) -> dict[str, Any]:
    total = int(connection.execute("SELECT COUNT(*) FROM candidate_memories").fetchone()[0])
    user_candidates = int(
        connection.execute("SELECT COUNT(*) FROM candidate_memories WHERE source_role = 'user'").fetchone()[0]
    )
    assistant_candidates = int(
        connection.execute("SELECT COUNT(*) FROM candidate_memories WHERE source_role = 'assistant'").fetchone()[0]
    )
    density = (user_candidates / total_chunks * 1000.0) if total_chunks else 0.0
    return {
        "total": total,
        "user_candidates": user_candidates,
        "assistant_candidates": assistant_candidates,
        "per_1000_chunks": round(density, 2),
        "memory_type_counts": _group_counts(
            connection,
            "SELECT memory_type AS label, COUNT(*) AS count FROM candidate_memories GROUP BY memory_type ORDER BY count DESC, label ASC",
        ),
        "review_status_counts": _group_counts(
            connection,
            "SELECT review_status AS label, COUNT(*) AS count FROM candidate_memories GROUP BY review_status ORDER BY count DESC, label ASC",
        ),
        "domain_counts": _group_counts(
            connection,
            "SELECT domain_primary AS label, COUNT(*) AS count FROM candidate_memories GROUP BY domain_primary ORDER BY count DESC, label ASC",
        ),
    }


def _codex_stats(connection: sqlite3.Connection) -> dict[str, int]:
    pattern = "%codex%"
    return {
        "title_conversations": int(
            connection.execute("SELECT COUNT(*) FROM conversations WHERE LOWER(title) LIKE ?", (pattern,)).fetchone()[0]
        ),
        "message_hits": int(
            connection.execute("SELECT COUNT(*) FROM messages WHERE LOWER(content_text) LIKE ?", (pattern,)).fetchone()[0]
        ),
        "conversation_hits": int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT messages.conversation_id)
                FROM messages
                WHERE LOWER(messages.content_text) LIKE ?
                """,
                (pattern,),
            ).fetchone()[0]
        ),
        "chunk_hits": int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM message_chunks
                JOIN messages ON messages.id = message_chunks.message_id
                WHERE LOWER(message_chunks.text) LIKE ?
                """,
                (pattern,),
            ).fetchone()[0]
        ),
    }


def _imports(connection: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, status, imported_at, conversation_count, message_count, chunk_count, source_root
        FROM imports
        ORDER BY imported_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "status": row[1],
            "imported_at": row[2],
            "conversation_count": int(row[3] or 0),
            "message_count": int(row[4] or 0),
            "chunk_count": int(row[5] or 0),
            "source_root": row[6],
        }
        for row in rows
    ]


def _group_counts(connection: sqlite3.Connection, query: str) -> list[dict[str, Any]]:
    rows = connection.execute(query).fetchall()
    return [{"label": row[0], "count": int(row[1] or 0)} for row in rows]


def _card(label: str, value: str) -> str:
    return f"<div class='metric'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div></div>"


def _fmt_float(value: Any) -> str:
    if value is None:
        return "0"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def analyze_memory_patterns(
    *,
    data_dir: Path,
    memory_dir: Path,
    logs_dir: Path,
    focus: str = "all",
    source_role: str = "user",
    limit: int = 2000,
    category_limit: int = 6,
    title_limit: int = 20,
) -> dict[str, Any]:
    sqlite_path = memory_db_path(memory_dir)
    report: dict[str, Any] = {
        "status": "warn",
        "checked_at": utc_now(),
        "data_dir": str(data_dir),
        "memory_dir": str(memory_dir),
        "sqlite_path": str(sqlite_path),
        "focus": focus,
        "source_role": source_role,
        "limit": limit,
        "category_limit": category_limit,
        "title_limit": title_limit,
        "pattern_sets": [],
        "recent_runs": list_recent_runs(logs_dir, limit=5),
    }
    if not sqlite_path.exists():
        report["error"] = {
            "stage": "analyze_patterns",
            "error_code": "memory_database_not_found",
            "message": f"ChatGPT memory database does not exist: {sqlite_path}",
        }
        return report

    requested_foci = _normalize_pattern_focus(focus)
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        for item in requested_foci:
            rows = _pattern_rows(
                connection,
                item,
                source_role=source_role,
                limit=limit,
            )
            report["pattern_sets"].append(
                _build_pattern_set(
                    item,
                    rows,
                    category_limit=category_limit,
                    title_limit=title_limit,
                )
            )
        report["status"] = "ok"
    return report


def render_memory_patterns(report: dict[str, Any]) -> str:
    lines = [
        f"Status: {report['status']}",
        f"Checked: {report['checked_at']}",
        f"Focus: {report.get('focus', 'all')}",
        f"Source role: {report.get('source_role', 'user')}",
        f"Data: {report['data_dir']}",
        f"Memory DB: {report['sqlite_path']}",
    ]
    for pattern_set in report.get("pattern_sets", []):
        lines.append("")
        lines.append(f"{pattern_set['label']}:")
        lines.append(
            f"- candidates={pattern_set['candidate_count']} titles={pattern_set['distinct_title_count']} domains={_fmt_counter(pattern_set.get('domain_counts', []))}"
        )
        if pattern_set.get("top_title_tokens"):
            lines.append(f"- top_title_tokens={_fmt_counter(pattern_set['top_title_tokens'][:8])}")
        if pattern_set.get("top_title_bigrams"):
            lines.append(f"- top_title_bigrams={_fmt_counter(pattern_set['top_title_bigrams'][:8])}")
        if pattern_set.get("top_titles"):
            lines.append("- top_titles:")
            for item in pattern_set["top_titles"]:
                lines.append(f"  - {item['title']} ({item['count']})")
        if pattern_set.get("suggested_categories"):
            lines.append("- natural_categories:")
            for category in pattern_set["suggested_categories"]:
                examples = ", ".join(category.get("examples", []))
                lines.append(
                    f"  - {category['name']} ({category['count']}): {examples}"
                )
    recent_runs = report.get("recent_runs", [])
    lines.append("")
    lines.append("Recent Runs:")
    if not recent_runs:
        lines.append("- none")
    else:
        for item in recent_runs:
            lines.append(
                f"- {item['run_id']} [{item.get('status')}] {item.get('command')} started={item.get('started_at')} finished={item.get('finished_at')}"
            )
    return "\n".join(lines)


def render_memory_patterns_html(report: dict[str, Any]) -> str:
    def section(title: str, body: str) -> str:
        return f"<section class='card'><h2>{html.escape(title)}</h2>{body}</section>"

    def table(headers: list[str], rows: list[list[str]]) -> str:
        head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    pattern_sets = report.get("pattern_sets", [])
    cards = "".join(
        _card(label, value)
        for label, value in [
            ("Status", str(report.get("status", "unknown"))),
            ("Focus", str(report.get("focus", "all"))),
            ("Source role", str(report.get("source_role", "user"))),
            ("Slices", str(len(pattern_sets))),
        ]
    )

    sections: list[str] = []
    for pattern_set in pattern_sets:
        category_rows = [
            [category["name"], str(category["count"]), ", ".join(category.get("examples", []))]
            for category in pattern_set.get("suggested_categories", [])
        ]
        title_rows = [
            [item["title"], str(item["count"])]
            for item in pattern_set.get("top_titles", [])
        ]
        sections.append(
            section(
                pattern_set["label"],
                (
                    f"<p class='muted'>Candidates: {pattern_set['candidate_count']} | Titles: {pattern_set['distinct_title_count']}</p>"
                    f"<h3>Natural categories</h3>"
                    f"{table(['Category', 'Count', 'Examples'], category_rows) if category_rows else '<p class=\"muted\">No categories inferred.</p>'}"
                    f"<h3>Top titles</h3>"
                    f"{table(['Title', 'Count'], title_rows) if title_rows else '<p class=\"muted\">No titles found.</p>'}"
                    f"<h3>Token signals</h3>"
                    f"{table(['Token', 'Count'], [[item['token'], str(item['count'])] for item in pattern_set.get('top_title_tokens', [])[:10]]) if pattern_set.get('top_title_tokens') else '<p class=\"muted\">No token signals found.</p>'}"
                ),
            )
        )

    recent_runs = report.get("recent_runs", [])
    recent_rows = [
        [item["run_id"], item.get("status") or "", item.get("command") or "", item.get("started_at") or "", item.get("finished_at") or ""]
        for item in recent_runs
    ]

    body = f"""
    <html>
      <head>
        <meta charset='utf-8' />
        <meta name='viewport' content='width=device-width, initial-scale=1' />
        <title>Memory Pattern Analysis</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f5f2ea;
            --panel: #fffdf8;
            --text: #1d1a16;
            --muted: #6b6257;
            --accent: #7b4f2a;
            --border: #d7cdbf;
          }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(180deg, #f8f4ec 0%, #efe7da 100%);
            color: var(--text);
          }}
          .wrap {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
          .hero {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: end; justify-content: space-between; margin-bottom: 20px; }}
          .hero h1 {{ margin: 0; font-size: 2rem; }}
          .hero p {{ margin: 6px 0 0; color: var(--muted); }}
          .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 20px; }}
          .metric {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 14px; box-shadow: 0 10px 20px rgba(68, 52, 35, 0.05); }}
          .metric .label {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
          .metric .value {{ font-size: 1.5rem; margin-top: 4px; font-weight: 650; }}
          .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 16px; padding: 18px; margin-bottom: 16px; box-shadow: 0 12px 26px rgba(68, 52, 35, 0.05); }}
          h2 {{ margin: 0 0 12px; font-size: 1.1rem; }}
          h3 {{ margin: 12px 0 8px; font-size: 1rem; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 0.95rem; }}
          th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }}
          th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; }}
          .muted {{ color: var(--muted); }}
          code {{ background: #f1e7d9; padding: 2px 6px; border-radius: 8px; }}
        </style>
      </head>
      <body>
        <div class='wrap'>
          <div class='hero'>
            <div>
              <h1>Memory Pattern Analysis</h1>
              <p>{html.escape(str(report.get("checked_at", "")))}</p>
              <p class='muted'>{html.escape(str(report.get("sqlite_path", "")))}</p>
            </div>
          </div>
          <div class='grid'>{cards}</div>
          {''.join(sections)}
          {section("Recent Runs", table(["Run", "Status", "Command", "Started", "Finished"], recent_rows) if recent_rows else "<p class='muted'>No recent runs.</p>")}
        </div>
      </body>
    </html>
    """
    return body


def _normalize_pattern_focus(focus: str) -> list[str]:
    normalized = focus.strip().lower()
    if normalized in {"all", "both"}:
        return ["recipes", "projects"]
    if normalized in {"recipe", "recipes", "cook", "cooking"}:
        return ["recipes"]
    if normalized in {"project", "projects", "build", "building"}:
        return ["projects"]
    raise ValueError(f"invalid pattern focus: {focus}")


def _pattern_rows(
    connection: sqlite3.Connection,
    focus: str,
    *,
    source_role: str,
    limit: int,
) -> list[sqlite3.Row]:
    where: list[str] = ["cm.source_role = ?"]
    params: list[Any] = [source_role]
    if focus == "recipes":
        where.append(
            """(
                cm.domain_primary = 'cooking_baking'
                OR lower(c.title || ' ' || cm.content) LIKE '%recipe%'
                OR lower(c.title || ' ' || cm.content) LIKE '%bake%'
                OR lower(c.title || ' ' || cm.content) LIKE '%dessert%'
                OR lower(c.title || ' ' || cm.content) LIKE '%curry%'
            )"""
        )
    elif focus == "projects":
        where.append(
            """(
                cm.domain_primary IN ('career_work', 'lab_automation', 'ai_memory_systems', 'home_projects')
                OR lower(c.title || ' ' || cm.content) LIKE '%project%'
                OR lower(c.title || ' ' || cm.content) LIKE '%workflow%'
                OR lower(c.title || ' ' || cm.content) LIKE '%automation%'
                OR lower(c.title || ' ' || cm.content) LIKE '%tool%'
            )"""
        )
    else:
        raise ValueError(f"invalid pattern focus: {focus}")

    limit_clause = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)
    rows = connection.execute(
        f"""
        SELECT
            cm.id,
            cm.domain_primary,
            cm.memory_type,
            cm.content,
            cm.assistant_suggestion,
            cm.metadata_json,
            c.title
        FROM candidate_memories cm
        JOIN conversations c ON c.id = cm.conversation_id
        WHERE {' AND '.join(where)}
        ORDER BY cm.updated_at DESC, cm.id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return rows


def _build_pattern_set(
    focus: str,
    rows: list[sqlite3.Row],
    *,
    category_limit: int,
    title_limit: int,
) -> dict[str, Any]:
    titles = Counter()
    title_tokens = Counter()
    title_bigrams = Counter()
    domain_counts = Counter()
    memory_type_counts = Counter()
    categories = Counter()
    category_examples: dict[str, list[str]] = {}

    for row in rows:
        title = str(row[6] or "").strip()
        content = str(row[3] or "").strip()
        domain = str(row[1] or "misc")
        memory_type = str(row[2] or "episodic")
        domain_counts[domain] += 1
        memory_type_counts[memory_type] += 1
        if title:
            titles[title] += 1
            tokens = _pattern_tokens(title)
            title_tokens.update(tokens)
            title_bigrams.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
        category = _pattern_category(focus, title=title, content=content)
        if category is not None:
            categories[category] += 1
            examples = category_examples.setdefault(category, [])
            if title and title not in examples and len(examples) < 3:
                examples.append(title)

    category_rows = [
        {
            "name": name,
            "count": count,
            "examples": category_examples.get(name, []),
            "signals": _pattern_category_signals(focus, name),
        }
        for name, count in categories.most_common(category_limit)
    ]

    top_titles = [{"title": title, "count": count} for title, count in titles.most_common(title_limit)]
    return {
        "focus": focus,
        "label": "Recipe landscape" if focus == "recipes" else "Project landscape",
        "candidate_count": len(rows),
        "distinct_title_count": len(titles),
        "domain_counts": [{"label": label, "count": count} for label, count in domain_counts.most_common()],
        "memory_type_counts": [{"label": label, "count": count} for label, count in memory_type_counts.most_common()],
        "top_titles": top_titles,
        "top_title_tokens": [{"token": token, "count": count} for token, count in title_tokens.most_common(20)],
        "top_title_bigrams": [{"token": token, "count": count} for token, count in title_bigrams.most_common(20)],
        "suggested_categories": category_rows,
    }


def _pattern_tokens(text: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.lower())
    tokens = [
        token
        for token in cleaned.split()
        if token and token not in _PATTERN_STOPWORDS and len(token) > 2
    ]
    return tokens


def _pattern_category(focus: str, *, title: str, content: str) -> str | None:
    text = f"{title} {content}".lower()
    if focus == "recipes":
        if any(term in text for term in ("cocktail", "martini", "drink", "mocktail")):
            return "drinks_and_cocktails"
        if any(term in text for term in ("bake", "baking", "dessert", "pie", "bars", "cake", "eclair", "brulee", "pastry", "cookie", "chocolate")):
            return "baking_and_desserts"
        if any(term in text for term in ("meal prep", "lunch", "dinner", "meal", "supper", "savory", "curry", "salmon", "khachapuri", "pork shoulder")):
            return "savory_meals"
        if any(term in text for term in ("timing", "temp", "temperature", "piping", "skill", "challenge", "prep ideas", "next best", "fun dish")):
            return "timing_and_technique"
        if any(term in text for term in ("recipe app", "recipe", "ideas", "suggest", "generate")):
            return "recipe_ideas_and_generation"
        return "misc_recipe"
    if focus == "projects":
        if any(term in text for term in ("resume", "cover letter", "job search", "interview", "career", "role overview", "job fit")):
            return "career_and_search"
        if any(term in text for term in ("lab automation", "liquid handling", "plate reader", "well selection", "barTender", "zebra", "hamilton", "worklist")):
            return "lab_automation"
        if any(term in text for term in ("vba", "vbscript", "excel", "userform", "macro", "csv", "selection", "macro automation")):
            return "spreadsheet_and_macros"
        if any(term in text for term in ("memory", "ollama", "rag", "embedding", "retrieval", "subject", "llm", "prompt")):
            return "ai_memory_systems"
        if any(term in text for term in ("home", "garage", "sensor", "aquarium", "heater", "shaker", "vpn ssh", "tv", "tool")):
            return "home_and_hardware"
        if any(term in text for term in ("analysis", "log file", "briefing", "dashboard", "highlighter", "report")):
            return "analysis_and_reporting"
        if any(term in text for term in ("build", "project", "prototype", "system", "workflow", "app", "tool")):
            return "general_builds"
        return "misc_project"
    return None


def _pattern_category_signals(focus: str, category: str) -> list[str]:
    if focus == "recipes":
        return {
            "drinks_and_cocktails": ["cocktail", "martini", "drink"],
            "savory_meals": ["meal prep", "dinner", "curry", "salmon"],
            "baking_and_desserts": ["bake", "dessert", "pie", "bars", "cake", "eclair", "brulee"],
            "timing_and_technique": ["timing", "temp", "piping", "challenge"],
            "recipe_ideas_and_generation": ["recipe app", "ideas", "generate"],
            "misc_recipe": [],
        }.get(category, [])
    return {
        "career_and_search": ["resume", "job search", "interview", "cover letter"],
        "lab_automation": ["lab automation", "liquid handling", "worklist", "zebra"],
        "spreadsheet_and_macros": ["vba", "vbscript", "excel", "userform"],
        "ai_memory_systems": ["memory", "ollama", "rag", "retrieval"],
        "home_and_hardware": ["home", "sensor", "aquarium", "heater"],
        "analysis_and_reporting": ["analysis", "dashboard", "log file", "report"],
        "general_builds": ["build", "project", "workflow", "tool"],
        "misc_project": [],
    }.get(category, [])


_PATTERN_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "what",
    "how",
    "to",
    "a",
    "an",
    "of",
    "in",
    "on",
    "my",
    "your",
    "our",
    "is",
    "are",
    "be",
    "it",
    "or",
    "we",
    "you",
    "me",
    "do",
    "can",
    "should",
    "would",
    "need",
    "use",
    "recipe",
    "recipes",
    "project",
    "projects",
    "build",
    "building",
    "idea",
    "ideas",
    "app",
    "plan",
    "plans",
    "make",
    "want",
    "help",
    "new",
    "old",
    "best",
    "easy",
    "response",
    "continuation",
    "advice",
    "next",
    "based",
    "chat",
    "response",
    "role",
}


def _fmt_counter(items: list[dict[str, Any]]) -> str:
    return ", ".join(f"{item['label'] if 'label' in item else item['token']}={item['count']}" for item in items)
