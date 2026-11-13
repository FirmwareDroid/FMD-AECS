import sqlite3
import time
import logging
from contextlib import contextmanager

DB_PATH = "fmd_reloaded_queue.db"


@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        yield conn
    finally:
        conn.close()


def _run_with_retry(fn, max_retries=8, base_delay=0.25):
    """Retries a SQLite operation on transient lock/busy errors with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < max_retries:
                delay = min(5.0, base_delay * (2 ** attempt))
                logging.warning(f"SQLite database locked ({e}); retrying in {delay:.2f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(delay)
                continue
            raise


def init_db():
    """Initializes the job queue table and enables WAL mode for concurrent access."""
    def _op():
        with get_db_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                         CREATE TABLE IF NOT EXISTS emulator_jobs
                         (
                             id
                             INTEGER
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             image_name
                             TEXT
                             NOT
                             NULL,
                             status
                             TEXT
                             DEFAULT
                             'PENDING',
                             created_at
                             TIMESTAMP
                             DEFAULT
                             CURRENT_TIMESTAMP
                         )
                         """)
            conn.commit()
    _run_with_retry(_op)


def push_job(image_name):
    """Pushes a new emulator image to the queue and returns its job ID."""
    def _op():
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO emulator_jobs (image_name) VALUES (?)", (image_name,))
            conn.commit()
            return cursor.lastrowid
    return _run_with_retry(_op)


def get_active_job_count(job_ids=None):
    """Returns the count of jobs currently in PENDING or PROCESSING state."""
    def _op():
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if job_ids is not None:
                if not job_ids:
                    return 0
                placeholders = ",".join("?" for _ in job_ids)
                cursor.execute(
                    f"SELECT COUNT(*) FROM emulator_jobs WHERE status IN ('PENDING', 'PROCESSING') AND id IN ({placeholders})",
                    tuple(job_ids),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM emulator_jobs WHERE status IN ('PENDING', 'PROCESSING')"
                )
            row = cursor.fetchone()
            return row[0] if row else 0
    return _run_with_retry(_op)


def get_queue_status_counts():
    """Returns a dictionary mapping job status to count."""
    def _op():
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) as cnt FROM emulator_jobs GROUP BY status")
            return {row["status"]: row["cnt"] for row in cursor.fetchall()}
    return _run_with_retry(_op)


def get_all_job_names():
    """Returns a set of all image_name values currently in emulator_jobs."""
    def _op():
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT image_name FROM emulator_jobs")
            return {row["image_name"] for row in cursor.fetchall() if row["image_name"]}
    return _run_with_retry(_op)


def fetch_next_job():
    """Atomically fetches and locks the next pending job."""
    def _op():
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN EXCLUSIVE")
            cursor.execute(
                "SELECT id, image_name FROM emulator_jobs WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT 1")
            job = cursor.fetchone()

            if job:
                cursor.execute("UPDATE emulator_jobs SET status = 'PROCESSING' WHERE id = ?", (job['id'],))
                conn.commit()
                return dict(job)

            conn.commit()
            return None
    return _run_with_retry(_op)


def mark_job_completed(job_id):
    """Marks a job as completed."""
    def _op():
        with get_db_connection() as conn:
            conn.execute("UPDATE emulator_jobs SET status = 'COMPLETED' WHERE id = ?", (job_id,))
            conn.commit()
    _run_with_retry(_op)