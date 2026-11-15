"""A Neon stand-in that actually runs the store's SQL, on SQLite.

The string-matching fakes prove what was ASKED; this one proves what the
database is left holding, which is what a cap or a sweep is for. Only the
handful of Postgres spellings the store uses are translated: ``$n``
placeholders, ``now()``, ``interval 'n seconds'`` and ``EXTRACT(EPOCH ...)``.
Time is a number the test moves, so a two-hour TTL needs no sleeping.
"""

from __future__ import annotations

import re
import sqlite3


class SqliteNeon:
    def __init__(self, cipher=None, clock: float = 1_700_000_000.0) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH ':memory:' AS op")
        self.clock = clock
        self.sql: list[str] = []
        if cipher is not None:
            self._cipher = cipher

    def _t(self, table: str) -> str:
        return f"op.{table}"

    def _translate(self, sql: str) -> str:
        sql = re.sub(r"interval '(\d+) seconds'", r"\1", sql)
        sql = re.sub(r"EXTRACT\(EPOCH FROM ([\w.]+)\)", r"\1", sql)
        sql = sql.replace("now()", repr(self.clock))
        sql = sql.replace("gen_random_uuid()", "(lower(hex(randomblob(16))))")
        sql = re.sub(r"DEFAULT ([\d.]+)", r"DEFAULT (\1)", sql)
        sql = sql.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
        # SQLite names an index's schema on the index, not the table.
        sql = re.sub(r"CREATE INDEX IF NOT EXISTS (\w+) ON op\.", r"CREATE INDEX IF NOT EXISTS op.\1 ON ", sql)
        return re.sub(r"\$(\d+)", r"?\1", sql)

    def _has(self, kind: str, name: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM op.sqlite_master WHERE type = ? AND name = ?", [kind, name]).fetchone())

    def _alter_if_exists(self, sql: str) -> bool:
        """Postgres renames a table or index only if it exists; SQLite has no such clause, so do it here."""
        m = re.fullmatch(r"ALTER (TABLE|INDEX) IF EXISTS op\.(\w+) RENAME TO (\w+)", sql.strip())
        if not m:
            return False
        kind, old, new = m.group(1).lower(), m.group(2), m.group(3)
        if self._has(kind, old):
            # SQLite cannot rename an index; dropping it lets the CREATE INDEX that follows remake it.
            self.db.execute(f"ALTER TABLE op.{old} RENAME TO {new}" if kind == "table" else f"DROP INDEX op.{old}")
        return True

    async def _execute(self, sql: str, params=None):
        self.sql.append(sql)
        if self._alter_if_exists(sql):
            return {"rows": []}
        try:
            cur = self.db.execute(self._translate(sql), params or [])
        except sqlite3.OperationalError as exc:
            if "duplicate column" in str(exc):
                return {"rows": []}
            raise
        return {"rows": [dict(r) for r in cur.fetchall()]}

    def rows(self, table: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(f"SELECT * FROM op.{table}")]

    def tables(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT name FROM op.sqlite_master WHERE type = 'table'")}
