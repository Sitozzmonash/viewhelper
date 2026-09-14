"""SQLite persistence (source of truth, PRD 19.1).

Design notes
------------
* WAL journal + ``busy_timeout`` so the ASR worker threads and the asyncio event
  loop can share one connection safely (guarded by an ``RLock``).
* All timestamps are UTC epoch milliseconds.
* ``history_payload`` builds the ``history_sync_response`` payload, including a
  preview data URL **only** for the latest screenshot.
* Deleting screenshots returns the on-disk paths so the caller can remove the
  image files too (PRD 18.2).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from src.storage.models import (
    AiAnswerRecord,
    AnswerMode,
    AnswerStatus,
    AudioSource,
    MessageRecord,
    ScreenshotRecord,
    SessionRecord,
    Speaker,
)
from src.util.clock import utc_now_ms
from src.util.files import ensure_dir, file_to_data_url
from src.util.log import get_logger

__all__ = ["Database", "SCHEMA", "new_id"]

_logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    title       TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    session_id    TEXT,
    utterance_id  TEXT,
    speaker       TEXT NOT NULL CHECK (speaker IN ('me', 'other')),
    source        TEXT NOT NULL CHECK (source IN ('mic', 'system')),
    text          TEXT NOT NULL,
    is_final      INTEGER NOT NULL DEFAULT 1,
    started_at    INTEGER,
    ended_at      INTEGER,
    created_at    INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_utterance ON messages (utterance_id)
    WHERE utterance_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ai_answers (
    id                 TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL,
    session_id         TEXT,
    mode               TEXT NOT NULL CHECK (mode IN ('conversation', 'screenshot')),
    target_id          TEXT,
    model              TEXT,
    answer             TEXT NOT NULL DEFAULT '',
    completion_tokens  INTEGER,
    estimated_tokens   INTEGER,
    elapsed_ms         INTEGER,
    tokens_per_second  REAL,
    status             TEXT NOT NULL CHECK (status IN ('done', 'cancelled', 'error')),
    error_message      TEXT,
    created_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_answers_created ON ai_answers (created_at);
CREATE INDEX IF NOT EXISTS idx_ai_answers_request ON ai_answers (request_id);
CREATE INDEX IF NOT EXISTS idx_ai_answers_target ON ai_answers (target_id);

CREATE TABLE IF NOT EXISTS screenshots (
    id             TEXT PRIMARY KEY,
    session_id     TEXT,
    local_path     TEXT NOT NULL,
    preview_path   TEXT,
    preview_mime   TEXT,
    preview_bytes  INTEGER,
    width          INTEGER,
    height         INTEGER,
    model          TEXT,
    trigger        TEXT NOT NULL DEFAULT 'hotkey',
    created_at     INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_screenshots_created ON screenshots (created_at);
"""


def new_id(prefix: str) -> str:
    """Short unique identifier, e.g. ``m-1f0c...``."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class Database:
    """Thread-safe SQLite wrapper for sessions/messages/answers/screenshots."""

    def __init__(
        self,
        path: Path | str,
        *,
        session_title: str | None = None,
        start_session: bool = True,
    ) -> None:
        self._path = Path(path)
        ensure_dir(self._path.parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=30.0,
            detect_types=0,
        )
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
        self._session: SessionRecord | None = None
        if start_session:
            self._session = self.create_session(title=session_title)

    # -- lifecycle ------------------------------------------------------
    @property
    def path(self) -> Path:
        """Database file location."""
        return self._path

    @property
    def session_id(self) -> str | None:
        """Currently active session id (``None`` when sessions are disabled)."""
        return self._session.id if self._session else None

    def close(self) -> None:
        """Flush and close the connection (idempotent)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- sessions -------------------------------------------------------
    def create_session(self, title: str | None = None) -> SessionRecord:
        """Open a new session row and make it current."""
        record = SessionRecord(
            id=new_id("s"),
            started_at=utc_now_ms(),
            ended_at=None,
            title=title,
        )
        self._execute(
            "INSERT INTO sessions (id, started_at, ended_at, title) VALUES (?, ?, ?, ?)",
            (record.id, record.started_at, record.ended_at, record.title),
        )
        self._session = record
        return record

    def end_session(self, ended_at: int | None = None) -> None:
        """Stamp ``ended_at`` on the current session."""
        if self._session is None:
            return
        stamp = ended_at if ended_at is not None else utc_now_ms()
        self._execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            (stamp, self._session.id),
        )
        self._session.ended_at = stamp

    # -- messages -------------------------------------------------------
    def insert_message(
        self,
        *,
        text: str,
        speaker: Speaker,
        source: AudioSource,
        utterance_id: str | None = None,
        started_at: int | None = None,
        ended_at: int | None = None,
        created_at: int | None = None,
        is_final: bool = True,
        message_id: str | None = None,
    ) -> MessageRecord:
        """Persist a final utterance.

        Idempotent on ``utterance_id``: a repeated final for the same utterance
        updates the existing row instead of creating a duplicate bubble (AC-02).
        """
        stamp = created_at if created_at is not None else utc_now_ms()
        if utterance_id:
            existing = self.find_message_by_utterance(utterance_id)
            if existing is not None:
                self._execute(
                    "UPDATE messages SET text = ?, is_final = ?, started_at = ?, ended_at = ? WHERE id = ?",
                    (text, int(is_final), started_at or existing.started_at, ended_at or stamp, existing.id),
                )
                existing.text = text
                existing.is_final = is_final
                existing.started_at = started_at or existing.started_at
                existing.ended_at = ended_at or stamp
                return existing
        record = MessageRecord(
            id=message_id or new_id("m"),
            speaker=speaker,
            source=source,
            text=text,
            created_at=stamp,
            session_id=self.session_id,
            utterance_id=utterance_id,
            is_final=is_final,
            started_at=started_at,
            ended_at=ended_at,
        )
        self._execute(
            """
            INSERT INTO messages (
                id, session_id, utterance_id, speaker, source, text,
                is_final, started_at, ended_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.session_id,
                record.utterance_id,
                record.speaker,
                record.source,
                record.text,
                int(record.is_final),
                record.started_at,
                record.ended_at,
                record.created_at,
            ),
        )
        return record

    def get_message(self, message_id: str) -> MessageRecord | None:
        """Fetch one message by id."""
        rows = self._query("SELECT * FROM messages WHERE id = ?", (message_id,), limit=1)
        return _row_to_message(rows[0]) if rows else None

    def find_message_by_utterance(self, utterance_id: str) -> MessageRecord | None:
        """Fetch a message by its ASR ``utterance_id``."""
        rows = self._query(
            "SELECT * FROM messages WHERE utterance_id = ? ORDER BY created_at DESC",
            (utterance_id,),
            limit=1,
        )
        return _row_to_message(rows[0]) if rows else None

    def recent_messages(self, limit: int = 100, *, final_only: bool = True) -> list[MessageRecord]:
        """Last *limit* messages, oldest first (chronological for the UI)."""
        clause = "WHERE is_final = 1" if final_only else ""
        rows = self._query(
            f"SELECT * FROM messages {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [_row_to_message(row) for row in reversed(rows)]

    def context_window(
        self, target_id: str, limit: int
    ) -> tuple[list[MessageRecord], MessageRecord | None]:
        """``(context, target)`` for an LLM conversation request (PRD 9).

        *context* holds up to *limit* final messages that precede the target, in
        chronological order, and never contains the target itself.
        """
        rows = self._query("SELECT rowid AS rid, * FROM messages WHERE id = ?", (target_id,), limit=1)
        if not rows:
            return [], None
        target = _row_to_message(rows[0])
        rowid = rows[0]["rid"]
        context_rows = self._query(
            """
            SELECT * FROM messages
            WHERE is_final = 1
              AND (created_at < ? OR (created_at = ? AND rowid < ?))
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (target.created_at, target.created_at, rowid, max(0, int(limit))),
        )
        return [_row_to_message(row) for row in reversed(context_rows)], target

    def clear_messages(self) -> int:
        """Delete every conversation message (AC-12); returns rows removed."""
        return self._execute("DELETE FROM messages")

    def count_messages(self) -> int:
        """Number of stored messages (diagnostics/tests)."""
        rows = self._query("SELECT COUNT(*) AS n FROM messages")
        return int(rows[0]["n"]) if rows else 0

    # -- ai answers -----------------------------------------------------
    def insert_ai_answer(
        self,
        *,
        request_id: str,
        mode: AnswerMode,
        answer: str,
        status: AnswerStatus,
        target_id: str | None = None,
        model: str | None = None,
        completion_tokens: int | None = None,
        estimated_tokens: int | None = None,
        elapsed_ms: int | None = None,
        tokens_per_second: float | None = None,
        error_message: str | None = None,
        created_at: int | None = None,
        answer_id: str | None = None,
    ) -> AiAnswerRecord:
        """Persist a finished/cancelled/failed answer (AC-07 keeps cancelled rows)."""
        record = AiAnswerRecord(
            id=answer_id or new_id("a"),
            request_id=request_id,
            mode=mode,
            answer=answer,
            status=status,
            created_at=created_at if created_at is not None else utc_now_ms(),
            session_id=self.session_id,
            target_id=target_id,
            model=model,
            completion_tokens=completion_tokens,
            estimated_tokens=estimated_tokens,
            elapsed_ms=elapsed_ms,
            tokens_per_second=tokens_per_second,
            error_message=error_message,
        )
        self._execute(
            """
            INSERT INTO ai_answers (
                id, request_id, session_id, mode, target_id, model, answer,
                completion_tokens, estimated_tokens, elapsed_ms, tokens_per_second,
                status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.request_id,
                record.session_id,
                record.mode,
                record.target_id,
                record.model,
                record.answer,
                record.completion_tokens,
                record.estimated_tokens,
                record.elapsed_ms,
                record.tokens_per_second,
                record.status,
                record.error_message,
                record.created_at,
            ),
        )
        return record

    def get_ai_answer(self, request_id: str) -> AiAnswerRecord | None:
        """Latest answer for a ``request_id``."""
        rows = self._query(
            "SELECT * FROM ai_answers WHERE request_id = ? ORDER BY created_at DESC, rowid DESC",
            (request_id,),
            limit=1,
        )
        return _row_to_answer(rows[0]) if rows else None

    def recent_ai_answers(self, limit: int = 100) -> list[AiAnswerRecord]:
        """Last *limit* answers, oldest first."""
        rows = self._query(
            "SELECT * FROM ai_answers ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [_row_to_answer(row) for row in reversed(rows)]

    def clear_ai_answers(self, mode: AnswerMode | None = None) -> int:
        """Delete answers, optionally restricted to one *mode*."""
        if mode is None:
            return self._execute("DELETE FROM ai_answers")
        return self._execute("DELETE FROM ai_answers WHERE mode = ?", (mode,))

    # -- screenshots ----------------------------------------------------
    def insert_screenshot(
        self,
        *,
        local_path: Path | str,
        screenshot_id: str | None = None,
        preview_path: Path | str | None = None,
        preview_mime: str | None = None,
        preview_bytes: int | None = None,
        width: int | None = None,
        height: int | None = None,
        model: str | None = None,
        trigger: str = "hotkey",
        created_at: int | None = None,
    ) -> ScreenshotRecord:
        """Persist screenshot metadata (paths only, never image bytes)."""
        record = ScreenshotRecord(
            id=screenshot_id or new_id("ss"),
            local_path=str(local_path),
            created_at=created_at if created_at is not None else utc_now_ms(),
            session_id=self.session_id,
            preview_path=str(preview_path) if preview_path else None,
            width=width,
            height=height,
            preview_bytes=preview_bytes,
            preview_mime=preview_mime,
            model=model,
            trigger=trigger,
        )
        self._execute(
            """
            INSERT INTO screenshots (
                id, session_id, local_path, preview_path, preview_mime, preview_bytes,
                width, height, model, trigger, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.session_id,
                record.local_path,
                record.preview_path,
                record.preview_mime,
                record.preview_bytes,
                record.width,
                record.height,
                record.model,
                record.trigger,
                record.created_at,
            ),
        )
        return record

    def get_screenshot(self, screenshot_id: str) -> ScreenshotRecord | None:
        """Fetch one screenshot row."""
        rows = self._query("SELECT * FROM screenshots WHERE id = ?", (screenshot_id,), limit=1)
        return _row_to_screenshot(rows[0]) if rows else None

    def latest_screenshot(self) -> ScreenshotRecord | None:
        """Most recent screenshot, if any."""
        rows = self._query("SELECT * FROM screenshots ORDER BY created_at DESC, rowid DESC", limit=1)
        return _row_to_screenshot(rows[0]) if rows else None

    def recent_screenshots(self, limit: int = 100) -> list[ScreenshotRecord]:
        """Last *limit* screenshots, newest first."""
        rows = self._query(
            "SELECT * FROM screenshots ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [_row_to_screenshot(row) for row in rows]

    def set_screenshot_model(self, screenshot_id: str, model: str | None) -> None:
        """Remember which vision model answered for a screenshot (PRD 13.4)."""
        self._execute("UPDATE screenshots SET model = ? WHERE id = ?", (model, screenshot_id))

    def delete_screenshot(self, screenshot_id: str) -> ScreenshotRecord | None:
        """Delete one row and return it (caller removes the files)."""
        record = self.get_screenshot(screenshot_id)
        if record is None:
            return None
        self._execute("DELETE FROM screenshots WHERE id = ?", (screenshot_id,))
        return record

    def clear_screenshots(self) -> list[ScreenshotRecord]:
        """Delete all screenshot rows, returning them so files can be removed."""
        records = self.recent_screenshots(limit=100_000)
        self._execute("DELETE FROM screenshots")
        return records

    def count_screenshots(self) -> int:
        """Number of stored screenshots (diagnostics/tests)."""
        rows = self._query("SELECT COUNT(*) AS n FROM screenshots")
        return int(rows[0]["n"]) if rows else 0

    # -- history sync ---------------------------------------------------
    def history_payload(self, limit: int = 100) -> dict[str, Any]:
        """Build the ``history_sync_response`` payload (PRD 5 of shared README).

        * messages / ai_answers: oldest first;
        * screenshots: newest first, with a preview data URL **only** on the
          first (latest) entry.
        """
        messages = [record.to_wire() for record in self.recent_messages(limit)]
        answers = [record.to_wire() for record in self.recent_ai_answers(limit)]
        screenshots: list[dict[str, Any]] = []
        for index, record in enumerate(self.recent_screenshots(limit)):
            preview = None
            if index == 0 and record.preview_path:
                preview = file_to_data_url(record.preview_path, record.preview_mime)
            screenshots.append(record.to_wire(preview))
        return {"messages": messages, "screenshots": screenshots, "ai_answers": answers}

    def screenshot_files(self, records: Iterable[ScreenshotRecord]) -> list[Path]:
        """Collect original + preview file paths of *records* for deletion."""
        paths: list[Path] = []
        for record in records:
            if record.local_path:
                paths.append(Path(record.local_path))
            if record.preview_path:
                paths.append(Path(record.preview_path))
        return paths

    # -- internals ------------------------------------------------------
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock, self._conn:
            try:
                cursor = self._conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                _logger.error("sqlite execute failed (%s): %s", sql.split()[0:2], exc)
                raise
            return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0

    def _query(self, sql: str, params: Sequence[Any] = (), limit: int | None = None) -> list[sqlite3.Row]:
        with self._lock:
            try:
                cursor = self._conn.execute(sql, tuple(params))
                rows = cursor.fetchall() if limit is None else cursor.fetchmany(limit)
            except sqlite3.Error as exc:
                _logger.error("sqlite query failed: %s", exc)
                raise
            return list(rows)


def _row_to_message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        id=row["id"],
        speaker=row["speaker"],
        source=row["source"],
        text=row["text"],
        created_at=int(row["created_at"]),
        session_id=row["session_id"],
        utterance_id=row["utterance_id"],
        is_final=bool(row["is_final"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
    )


def _row_to_screenshot(row: sqlite3.Row) -> ScreenshotRecord:
    return ScreenshotRecord(
        id=row["id"],
        local_path=row["local_path"],
        created_at=int(row["created_at"]),
        session_id=row["session_id"],
        preview_path=row["preview_path"],
        width=row["width"],
        height=row["height"],
        preview_bytes=row["preview_bytes"],
        preview_mime=row["preview_mime"],
        model=row["model"],
        trigger=row["trigger"] or "hotkey",
    )


def _row_to_answer(row: sqlite3.Row) -> AiAnswerRecord:
    return AiAnswerRecord(
        id=row["id"],
        request_id=row["request_id"],
        mode=row["mode"],
        answer=row["answer"],
        status=row["status"],
        created_at=int(row["created_at"]),
        session_id=row["session_id"],
        target_id=row["target_id"],
        model=row["model"],
        completion_tokens=row["completion_tokens"],
        estimated_tokens=row["estimated_tokens"],
        elapsed_ms=row["elapsed_ms"],
        tokens_per_second=row["tokens_per_second"],
        error_message=row["error_message"],
    )
