import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Set, Tuple


class FileTracker:
    """
    Persistent SQLite tracker for Telegram messages and media files.
    Prevents duplicate downloads and re-uploads across bot restarts using:
      1. Message identifiers: (chat_id, message_id)
      2. Telegram File IDs: document.id and photo.id (zero-download check)
      3. Content SHA-256 Hashes: cryptographic checksum of binary files
    """

    def __init__(self, db_path: str = "saveit_tracker.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
        if str(db_path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_db()

    def _init_db(self):
        """Initializes database schema and indices."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    message_id INTEGER,
                    telegram_file_id TEXT,
                    file_name TEXT,
                    file_size INTEGER,
                    file_hash TEXT,
                    caption TEXT,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg ON saved_records(chat_id, message_id);"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_id ON saved_records(telegram_file_id);"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hash ON saved_records(file_hash);"
            )

    def load_all_message_keys(self) -> Set[Tuple[int, int]]:
        """Loads all (chat_id, message_id) tuples into memory for instant O(1) checks."""
        with self._lock:
            cur = self._conn.execute("SELECT chat_id, message_id FROM saved_records;")
            return {(row[0], row[1]) for row in cur.fetchall()}

    def is_message_saved(self, chat_id: int, message_id: int) -> bool:
        """Checks if the message was already saved."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM saved_records WHERE chat_id = ? AND message_id = ? LIMIT 1;",
                (chat_id, message_id),
            )
            return cur.fetchone() is not None

    def is_file_id_saved(self, telegram_file_id: Optional[str]) -> bool:
        """Checks if Telegram's internal file ID (doc_id/photo_id) was already saved."""
        if not telegram_file_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM saved_records WHERE telegram_file_id = ? LIMIT 1;",
                (telegram_file_id,),
            )
            return cur.fetchone() is not None

    def is_file_hash_saved(self, file_hash: Optional[str]) -> bool:
        """Checks if an identical binary file (matching SHA-256) was already saved."""
        if not file_hash:
            return False
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM saved_records WHERE file_hash = ? LIMIT 1;",
                (file_hash,),
            )
            return cur.fetchone() is not None

    def record_saved(
        self,
        chat_id: int,
        message_id: int,
        telegram_file_id: Optional[str] = None,
        file_name: Optional[str] = None,
        file_size: Optional[int] = None,
        file_hash: Optional[str] = None,
        caption: Optional[str] = None,
    ):
        """Records a successfully saved message and its media metadata."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO saved_records 
                (chat_id, message_id, telegram_file_id, file_name, file_size, file_hash, caption)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    chat_id,
                    message_id,
                    telegram_file_id,
                    file_name,
                    file_size,
                    file_hash,
                    caption[:200] if caption else None,
                ),
            )

    @staticmethod
    def compute_sha256(file_path: str) -> str:
        """Computes SHA-256 hash of a file efficiently using 64KB chunks."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()

    def get_stats(self) -> dict:
        """Returns aggregate statistics about archived records and storage."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM saved_records;"
            )
            count, total_bytes = cur.fetchone()
            cur_hashes = self._conn.execute(
                "SELECT COUNT(DISTINCT file_hash) FROM saved_records WHERE file_hash IS NOT NULL;"
            )
            unique_hashes = cur_hashes.fetchone()[0]

            return {
                "total_records": count or 0,
                "total_bytes": total_bytes or 0,
                "unique_hashes": unique_hashes or 0,
                "db_path": str(self.db_path),
            }

    def close(self):
        """Closes the underlying SQLite database connection."""
        with self._lock:
            self._conn.close()


def extract_telegram_file_id(message) -> Optional[str]:
    """Extracts a stable Telegram media identifier from a message."""
    if not message or not getattr(message, "media", None):
        return None
    media = message.media
    if hasattr(media, "document") and media.document:
        return f"doc_{media.document.id}"
    if hasattr(media, "photo") and media.photo:
        return f"photo_{media.photo.id}"
    return None
