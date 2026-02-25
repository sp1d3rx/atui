from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


DEFAULT_HISTORY_DB_PATH = Path("port-history.db")
ACTIVE_STATUSES = ("active", "simulated-active")


@dataclass(slots=True, frozen=True)
class PortForwardRecord:
    record_id: str
    forward_name: str
    instance_id: str
    instance_name: str
    remote_port: int
    local_port: int
    started_at: str
    ended_at: str | None
    status: str
    command: str
    note: str | None = None


class PortForwardHistoryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_HISTORY_DB_PATH
        self._init_db()
        self._mark_unfinished_as_interrupted()

    def create(
        self,
        *,
        forward_name: str,
        instance_id: str,
        instance_name: str,
        remote_port: int,
        local_port: int,
        status: str,
        command: str,
        note: str | None = None,
    ) -> PortForwardRecord:
        record = PortForwardRecord(
            record_id=uuid4().hex,
            forward_name=forward_name.strip() or f"forward-{local_port}-to-{remote_port}",
            instance_id=instance_id,
            instance_name=instance_name,
            remote_port=remote_port,
            local_port=local_port,
            started_at=utc_now(),
            ended_at=None,
            status=status,
            command=command,
            note=note,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO port_forward_history (
                    record_id, forward_name, instance_id, instance_name, remote_port, local_port,
                    started_at, ended_at, status, command, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.forward_name,
                    record.instance_id,
                    record.instance_name,
                    record.remote_port,
                    record.local_port,
                    record.started_at,
                    record.ended_at,
                    record.status,
                    record.command,
                    record.note,
                ),
            )
        return record

    def update(self, record_id: str, **changes: object) -> PortForwardRecord | None:
        if not changes:
            return self.get(record_id)

        allowed = {
            "forward_name",
            "instance_id",
            "instance_name",
            "remote_port",
            "local_port",
            "started_at",
            "ended_at",
            "status",
            "command",
            "note",
        }
        columns: list[str] = []
        values: list[object] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            columns.append(f"{key} = ?")
            values.append(value)

        if not columns:
            return self.get(record_id)

        values.append(record_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE port_forward_history SET {', '.join(columns)} WHERE record_id = ?",
                values,
            )
            if cursor.rowcount == 0:
                return None
        return self.get(record_id)

    def get(self, record_id: str) -> PortForwardRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    record_id, forward_name, instance_id, instance_name, remote_port, local_port,
                    started_at, ended_at, status, command, note
                FROM port_forward_history
                WHERE record_id = ?
                """,
                (record_id,),
            ).fetchone()
        return self._record_from_row(row)

    def list_for_instance(self, instance_id: str) -> list[PortForwardRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    record_id, forward_name, instance_id, instance_name, remote_port, local_port,
                    started_at, ended_at, status, command, note
                FROM port_forward_history
                WHERE instance_id = ?
                ORDER BY started_at DESC
                """,
                (instance_id,),
            ).fetchall()
        return [record for row in rows if (record := self._record_from_row(row)) is not None]

    def list_all(self) -> list[PortForwardRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    record_id, forward_name, instance_id, instance_name, remote_port, local_port,
                    started_at, ended_at, status, command, note
                FROM port_forward_history
                ORDER BY started_at DESC
                """
            ).fetchall()
        return [record for row in rows if (record := self._record_from_row(row)) is not None]

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS port_forward_history (
                    record_id TEXT PRIMARY KEY,
                    forward_name TEXT NOT NULL DEFAULT '',
                    instance_id TEXT NOT NULL,
                    instance_name TEXT NOT NULL,
                    remote_port INTEGER NOT NULL,
                    local_port INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    command TEXT NOT NULL,
                    note TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_port_history_instance_started
                ON port_forward_history (instance_id, started_at DESC)
                """
            )
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(port_forward_history)").fetchall()
        columns = {str(row["name"]) for row in rows}
        if "forward_name" not in columns:
            conn.execute(
                "ALTER TABLE port_forward_history ADD COLUMN forward_name TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            """
            UPDATE port_forward_history
            SET forward_name = ('forward-' || local_port || '-to-' || remote_port)
            WHERE TRIM(COALESCE(forward_name, '')) = ''
            """
        )

    def _mark_unfinished_as_interrupted(self) -> None:
        now = utc_now()
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE port_forward_history
                SET status = ?, ended_at = ?, note = ?
                WHERE status IN ({placeholders}) AND ended_at IS NULL
                """,
                ("interrupted", now, "Marked interrupted after app restart.", *ACTIVE_STATUSES),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _record_from_row(row: sqlite3.Row | None) -> PortForwardRecord | None:
        if row is None:
            return None
        return PortForwardRecord(
            record_id=str(row["record_id"]),
            forward_name=_coerce_forward_name(
                str(row["forward_name"]) if row["forward_name"] else "",
                local_port=int(row["local_port"]),
                remote_port=int(row["remote_port"]),
            ),
            instance_id=str(row["instance_id"]),
            instance_name=str(row["instance_name"]),
            remote_port=int(row["remote_port"]),
            local_port=int(row["local_port"]),
            started_at=str(row["started_at"]),
            ended_at=str(row["ended_at"]) if row["ended_at"] else None,
            status=str(row["status"]),
            command=str(row["command"]),
            note=str(row["note"]) if row["note"] else None,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_forward_name(value: str, *, local_port: int, remote_port: int) -> str:
    stripped = value.strip()
    if stripped:
        return stripped
    return f"forward-{local_port}-to-{remote_port}"
