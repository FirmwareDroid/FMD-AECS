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
    """Pushes a new emulator image to the queue."""
    with get_db_connection() as conn:
        conn.execute("INSERT INTO emulator_jobs (image_name) VALUES (?)", (image_name,))
        conn.commit()


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