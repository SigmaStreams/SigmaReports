import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, Iterable


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _try_parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class ReportDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        self._payload_col = "payload_json"
        self._created_at_col = "created_at"

        self._ensure_schema()
        self._detect_reports_columns()

    # ---------------- Schema helpers ----------------

    def _table_columns(self, table: str) -> list[str]:
        cur = self.conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        return [r[1] for r in cur.fetchall()]

    def _ensure_column(self, table: str, col: str, decl: str) -> None:
        cols = self._table_columns(table)
        if col not in cols:
            cur = self.conn.cursor()
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.conn.commit()

    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                reporter_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                staff_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'Open',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_blocks (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                is_permanent INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                reason TEXT,
                blocked_by INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS liveboards (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plex_liveboards (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plex_statuses (
                guild_id INTEGER NOT NULL,
                server_name TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, server_name)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plex_manual_overrides (
                guild_id INTEGER NOT NULL,
                server_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, server_name)
            )
            """
        )

        self.conn.commit()

        # Newer features
        self._ensure_column("reports", "ticket_channel_id", "INTEGER")
        self._ensure_column("reports", "resolved_by", "INTEGER")
        self._ensure_column("reports", "resolved_at", "TEXT")
        self._ensure_column("plex_manual_overrides", "staff_message_id", "INTEGER")

        # Default setting values
        if self._get_setting("report_pings_enabled") is None:
            self._set_setting("report_pings_enabled", "1")

    def _detect_reports_columns(self) -> None:
        cols = self._table_columns("reports")

        if "payload_json" in cols:
            self._payload_col = "payload_json"
        elif "payload" in cols:
            self._payload_col = "payload"
        else:
            self._payload_col = "payload_json"

        self._created_at_col = "created_at" if "created_at" in cols else "created_at"
        print(f"DB: reports payload column = '{self._payload_col}', created_at column = '{self._created_at_col}'")

    # ---------------- Settings ----------------

    def _get_setting(self, key: str) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def _set_setting(self, key: str, value: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------------- Reports ----------------

    def create_report(self, report_type: str, reporter_id: int, guild_id: int, source_channel_id: int, payload: dict) -> int:
        payload_json = json.dumps(payload, ensure_ascii=False)
        now = _utcnow_iso()

        cur = self.conn.cursor()
        cur.execute(
            f"""
            INSERT INTO reports
            (report_type, reporter_id, guild_id, source_channel_id, {self._payload_col}, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Open', ?, ?)
            """,
            (report_type.upper(), reporter_id, guild_id, source_channel_id, payload_json, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_staff_message_id(self, report_id: int, message_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE reports SET staff_message_id=? WHERE id=?", (int(message_id), int(report_id)))
        self.conn.commit()

    def update_status(self, report_id: int, status: str) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE reports SET status=?, updated_at=? WHERE id=?", (status, _utcnow_iso(), int(report_id)))
        self.conn.commit()

    def mark_resolved(self, report_id: int, staff_user_id: int) -> None:
        now = _utcnow_iso()
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE reports
            SET status='Resolved',
                resolved_by=?,
                resolved_at=?,
                updated_at=?
            WHERE id=?
            """,
            (int(staff_user_id), now, now, int(report_id)),
        )
        self.conn.commit()

    def update_reporter_id(self, report_id: int, new_reporter_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE reports SET reporter_id=?, updated_at=? WHERE id=?",
            (int(new_reporter_id), _utcnow_iso(), int(report_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_by_id(self, report_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM reports WHERE id=?", (int(report_id),))
        return self._row_to_report(cur.fetchone())

    def get_report_by_id(self, report_id: int):
        return self.get_by_id(report_id)

    def get_by_staff_message_id(self, staff_message_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM reports WHERE staff_message_id=?", (int(staff_message_id),))
        return self._row_to_report(cur.fetchone())

    def _row_to_report(self, row):
        if not row:
            return None

        raw_payload = row[self._payload_col] if self._payload_col in row.keys() else None
        try:
            payload = json.loads(raw_payload) if raw_payload else {}
        except Exception:
            payload = {}

        out = {
            "id": row["id"],
            "report_type": row["report_type"],
            "reporter_id": row["reporter_id"],
            "guild_id": row["guild_id"],
            "source_channel_id": row["source_channel_id"],
            "payload": payload,
            "status": row["status"] if "status" in row.keys() else "Open",
            "staff_message_id": row["staff_message_id"] if "staff_message_id" in row.keys() else None,
            "created_at": row["created_at"] if "created_at" in row.keys() else None,
            "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
        }

        if "ticket_channel_id" in row.keys():
            out["ticket_channel_id"] = row["ticket_channel_id"]

        if "resolved_by" in row.keys():
            out["resolved_by"] = row["resolved_by"]

        if "resolved_at" in row.keys():
            out["resolved_at"] = row["resolved_at"]

        return out

    def list_active_reports(self, guild_id: int, closed_statuses: Optional[Iterable[str]] = None) -> list[dict]:
        closed = {s.strip() for s in (closed_statuses or []) if str(s).strip()}
        cur = self.conn.cursor()

        if closed:
            placeholders = ",".join("?" for _ in closed)
            params = [int(guild_id), *list(closed)]
            cur.execute(
                f"""
                SELECT *
                FROM reports
                WHERE guild_id=?
                  AND status NOT IN ({placeholders})
                ORDER BY id DESC
                """,
                params,
            )
        else:
            cur.execute(
                """
                SELECT *
                FROM reports
                WHERE guild_id=?
                ORDER BY id DESC
                """,
                (int(guild_id),),
            )

        return [self._row_to_report(r) for r in cur.fetchall() if r]

    # ---------------- Ticket helpers ----------------

    def get_ticket_channel_id(self, report_id: int) -> Optional[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT ticket_channel_id FROM reports WHERE id=?", (int(report_id),))
        row = cur.fetchone()
        if not row:
            return None
        val = row["ticket_channel_id"]
        return int(val) if val else None

    def set_ticket_channel_id(self, report_id: int, channel_id: Optional[int]) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE reports SET ticket_channel_id=? WHERE id=?", (channel_id, int(report_id)))
        self.conn.commit()

    # ---------------- Report pings ----------------

    def get_report_pings_enabled(self) -> bool:
        v = self._get_setting("report_pings_enabled")
        return v != "0"

    def toggle_report_pings(self) -> bool:
        enabled = self.get_report_pings_enabled()
        new_val = "0" if enabled else "1"
        self._set_setting("report_pings_enabled", new_val)
        return new_val == "1"

    # ---------------- Block system ----------------

    def block_user(
        self,
        guild_id: int,
        user_id: int,
        permanent: bool,
        duration_minutes: Optional[int] = None,
        reason: str = "",
        blocked_by: Optional[int] = None,
    ) -> None:
        expires_at = None
        if not permanent and duration_minutes:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=int(duration_minutes))).isoformat()

        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO user_blocks (guild_id, user_id, is_permanent, expires_at, reason, blocked_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET is_permanent=excluded.is_permanent,
                          expires_at=excluded.expires_at,
                          reason=excluded.reason,
                          blocked_by=excluded.blocked_by,
                          created_at=excluded.created_at
            """,
            (int(guild_id), int(user_id), 1 if permanent else 0, expires_at, reason, blocked_by, _utcnow_iso()),
        )
        self.conn.commit()

    def unblock_user(self, guild_id: int, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM user_blocks WHERE guild_id=? AND user_id=?", (int(guild_id), int(user_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def is_user_blocked(self, guild_id: int, user_id: int) -> tuple[bool, bool, Optional[str], str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT is_permanent, expires_at, reason FROM user_blocks WHERE guild_id=? AND user_id=?",
            (int(guild_id), int(user_id)),
        )
        row = cur.fetchone()
        if not row:
            return (False, False, None, "")

        is_perm = bool(row["is_permanent"])
        expires_at = row["expires_at"]
        reason = row["reason"] or ""

        if is_perm:
            return (True, True, None, reason)

        exp_dt = _try_parse_iso(expires_at)
        if exp_dt and exp_dt <= datetime.now(timezone.utc):
            self.unblock_user(guild_id, user_id)
            return (False, False, None, "")

        return (True, False, expires_at, reason)

    def list_blocked_users(self, guild_id: int) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT guild_id, user_id, is_permanent, expires_at, reason, blocked_by, created_at
            FROM user_blocks
            WHERE guild_id=?
            ORDER BY created_at DESC
            """,
            (int(guild_id),),
        )
        out = []
        for r in cur.fetchall():
            out.append(
                {
                    "guild_id": r["guild_id"],
                    "user_id": r["user_id"],
                    "is_permanent": bool(r["is_permanent"]),
                    "expires_at": r["expires_at"],
                    "reason": r["reason"] or "",
                    "blocked_by": r["blocked_by"],
                    "created_at": r["created_at"],
                }
            )
        return out

    # ---------------- Report liveboard ----------------

    def set_liveboard(self, guild_id: int, channel_id: int, message_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO liveboards (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id=excluded.channel_id,
                          message_id=excluded.message_id
            """,
            (int(guild_id), int(channel_id), int(message_id)),
        )
        self.conn.commit()

    def get_liveboard(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT guild_id, channel_id, message_id FROM liveboards WHERE guild_id=?", (int(guild_id),))
        row = cur.fetchone()
        if not row:
            return None
        return {"guild_id": row["guild_id"], "channel_id": row["channel_id"], "message_id": row["message_id"]}

    def list_liveboards(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT guild_id, channel_id, message_id FROM liveboards")
        rows = cur.fetchall()
        return [{"guild_id": r["guild_id"], "channel_id": r["channel_id"], "message_id": r["message_id"]} for r in rows]

    def clear_liveboard(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM liveboards WHERE guild_id=?", (int(guild_id),))
        self.conn.commit()

    # ---------------- Plex liveboard ----------------

    def set_plex_liveboard(self, guild_id: int, channel_id: int, message_id: int):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO plex_liveboards (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id=excluded.channel_id,
                          message_id=excluded.message_id
            """,
            (int(guild_id), int(channel_id), int(message_id)),
        )
        self.conn.commit()

    def get_plex_liveboard(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT guild_id, channel_id, message_id FROM plex_liveboards WHERE guild_id=?",
            (int(guild_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "guild_id": row["guild_id"],
            "channel_id": row["channel_id"],
            "message_id": row["message_id"],
        }

    def list_plex_liveboards(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT guild_id, channel_id, message_id FROM plex_liveboards")
        rows = cur.fetchall()
        return [
            {"guild_id": r["guild_id"], "channel_id": r["channel_id"], "message_id": r["message_id"]}
            for r in rows
        ]

    def clear_plex_liveboard(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plex_liveboards WHERE guild_id=?", (int(guild_id),))
        self.conn.commit()

    # ---------------- Plex statuses ----------------

    def set_plex_status(self, guild_id: int, server_name: str, status: str, updated_at: Optional[str] = None):
        now = updated_at or _utcnow_iso()
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO plex_statuses (guild_id, server_name, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, server_name)
            DO UPDATE SET status=excluded.status,
                          updated_at=excluded.updated_at
            """,
            (int(guild_id), str(server_name).upper(), str(status), now),
        )
        self.conn.commit()

    def set_plex_manual_override(
        self,
        guild_id: int,
        server_name: str,
        is_active: bool,
        staff_message_id: Optional[int] = None,
    ):
        cur = self.conn.cursor()
        server_name = str(server_name).upper()

        if is_active:
            cur.execute(
                """
                INSERT INTO plex_manual_overrides (guild_id, server_name, created_at, staff_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, server_name)
                DO UPDATE SET created_at=excluded.created_at,
                              staff_message_id=COALESCE(excluded.staff_message_id, plex_manual_overrides.staff_message_id)
                """,
                (int(guild_id), server_name, _utcnow_iso(), int(staff_message_id) if staff_message_id else None),
            )
        else:
            cur.execute(
                "DELETE FROM plex_manual_overrides WHERE guild_id=? AND server_name=?",
                (int(guild_id), server_name),
            )

        self.conn.commit()

    def has_plex_manual_override(self, guild_id: int, server_name: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT 1 FROM plex_manual_overrides WHERE guild_id=? AND server_name=?",
            (int(guild_id), str(server_name).upper()),
        )
        return cur.fetchone() is not None

    def get_plex_manual_override(self, guild_id: int, server_name: str) -> Optional[dict]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT guild_id, server_name, created_at, staff_message_id
            FROM plex_manual_overrides
            WHERE guild_id=? AND server_name=?
            """,
            (int(guild_id), str(server_name).upper()),
        )
        row = cur.fetchone()
        if not row:
            return None

        return {
            "guild_id": row["guild_id"],
            "server_name": row["server_name"],
            "created_at": row["created_at"],
            "staff_message_id": row["staff_message_id"],
        }

    def clear_plex_manual_overrides(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plex_manual_overrides WHERE guild_id=?", (int(guild_id),))
        self.conn.commit()

    def get_plex_statuses(self, guild_id: int) -> dict[str, str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT server_name, status FROM plex_statuses WHERE guild_id=?",
            (int(guild_id),),
        )
        rows = cur.fetchall()
        out: dict[str, str] = {}
        for r in rows:
            out[str(r["server_name"]).upper()] = str(r["status"])
        return out

    def clear_plex_statuses(self, guild_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM plex_statuses WHERE guild_id=?", (int(guild_id),))
        self.conn.commit()
