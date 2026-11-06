"""Book-list operations used by both the desktop page and the agent."""
import sqlite3
from contextlib import closing
from pathlib import Path


def actions(data: Path):
    def database():
        db = sqlite3.connect(data / "library.sqlite3", timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS books (title TEXT PRIMARY KEY, finished INTEGER NOT NULL DEFAULT 0)")
        return db

    def list_books() -> list[dict]:
        """List books and whether each has been read."""
        with closing(database()) as db:
            return [{"title": row[0], "finished": bool(row[1])}
                    for row in db.execute("SELECT title, finished FROM books ORDER BY rowid")]

    def add_book(title: str) -> list[dict]:
        """Add a book to the reading list."""
        title = title.strip()
        if not title:
            raise ValueError("请输入书名")
        with closing(database()) as db, db:
            db.execute("INSERT OR IGNORE INTO books(title) VALUES (?)", (title,))
        return list_books()

    def set_read(title: str, finished: bool) -> list[dict]:
        """Mark the named book as read or unread."""
        if type(finished) is not bool:
            raise ValueError("阅读状态须为布尔值")
        with closing(database()) as db, db:
            if not db.execute("UPDATE books SET finished=? WHERE title=?", (finished, title)).rowcount:
                raise ValueError("书单中没有这本书")
        return list_books()

    return {fn.__name__: fn for fn in (list_books, add_book, set_read)}
