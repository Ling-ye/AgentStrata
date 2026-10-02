"""Private cursor and conversation reply context; no business permissions."""

from pathlib import Path

from chatcopilot.core.private_sqlite import PrivateDatabase, private_directory


class WeixinState:
    def __init__(self, root: Path, account_id: str):
        self.account_id = account_id
        self._db = PrivateDatabase(
            private_directory(root) / "weixin.sqlite3",
            """
            CREATE TABLE cursor(account TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE contexts(account TEXT NOT NULL, peer TEXT NOT NULL, token TEXT NOT NULL,
                                  PRIMARY KEY(account,peer));
        """,
        )

    def cursor(self) -> str:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM cursor WHERE account=?", (self.account_id,)
            ).fetchone()
        return row[0] if row else ""

    def set_cursor(self, value: str) -> None:
        with self._db.connect(write=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO cursor VALUES (?,?)", (self.account_id, value)
            )

    def context(self, peer: str) -> str | None:
        with self._db.connect() as connection:
            row = connection.execute(
                "SELECT token FROM contexts WHERE account=? AND peer=?", (self.account_id, peer)
            ).fetchone()
        return row[0] if row else None

    def set_context(self, peer: str, token: str) -> None:
        with self._db.connect(write=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO contexts VALUES (?,?,?)", (self.account_id, peer, token)
            )
