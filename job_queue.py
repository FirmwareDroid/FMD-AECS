import sqlite3
import os
from contextlib import contextmanager

DB_PATH = "fmd_reloaded_queue.db"


@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initializes the job queue table."""
    with get_db_connection() as conn:
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


def push_job(image_name):
    """Pushes a new emulator image to the queue and returns its job ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO emulator_jobs (image_name) VALUES (?)", (image_name,))
        conn.commit()
        return cursor.lastrowid


def get_active_job_count(job_ids=None):
    """Returns the count of jobs currently in PENDING or PROCESSING state."""
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


def get_queue_status_counts():
    """Returns a dictionary mapping job status to count."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as cnt FROM emulator_jobs GROUP BY status")
        return {row["status"]: row["cnt"] for row in cursor.fetchall()}


def fetch_next_job():
    """Atomically fetches and locks the next pending job."""
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


def mark_job_completed(job_id):
    """Marks a job as completed."""
    with get_db_connection() as conn:
        conn.execute("UPDATE emulator_jobs SET status = 'COMPLETED' WHERE id = ?", (job_id,))
        conn.commit()