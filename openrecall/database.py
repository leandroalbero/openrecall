import os
import sqlite3
from collections import namedtuple
import numpy as np
from typing import List, Optional
from urllib.parse import urlparse

from openrecall.config import db_url
from openrecall.nlp import cosine_similarity

# Define the structure of a database entry using namedtuple
Entry = namedtuple("Entry", ["id", "app", "title", "text", "timestamp", "embedding", "filename", "ocr_data"])

def get_conn_params():
    parsed = urlparse(db_url)
    scheme = parsed.scheme
    if scheme == "sqlite":
        return {"scheme": "sqlite", "path": parsed.path}
    elif scheme == "postgresql":
        return {
            "scheme": "postgresql",
            "user": parsed.username,
            "password": parsed.password,
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "dbname": parsed.path.lstrip("/"),
        }
    else:
        raise ValueError(f"Unsupported database scheme: {scheme}")

params = get_conn_params()
scheme = params["scheme"]

if scheme == "sqlite":
    import sqlite3

    db_path = params["path"]
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    def get_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def serialize_embedding(embedding: np.ndarray):
        return embedding.astype(np.float32).tobytes()

    def deserialize_embedding(data):
        return np.frombuffer(data, dtype=np.float32)

    embedding_sql_type = "BLOB"

    def get_cursor(conn):
        return conn.cursor()
elif scheme == "postgresql":
    try:
        import psycopg2
        import psycopg2.extras
        from pgvector.psycopg2 import register_vector
    except ImportError as e:
        raise ImportError(
            "PostgreSQL support requires 'psycopg2-binary' and 'pgvector'. Install with 'pip install psycopg2-binary pgvector'"
        ) from e

    def init_connection():
        return psycopg2.connect(
            dbname=params["dbname"],
            user=params["user"],
            password=params["password"],
            host=params["host"],
            port=params["port"],
        )

    def get_connection():
        conn = init_connection()
        register_vector(conn)
        return conn

    def serialize_embedding(embedding: np.ndarray):
        return embedding

    def deserialize_embedding(data):
        return data

    embedding_sql_type = "vector(384)"

    def get_cursor(conn):
        return conn.cursor() if scheme == "sqlite" else conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

param_placeholder = "?" if scheme == "sqlite" else "%s"

def create_db() -> None:
    conn = init_connection() if scheme == "postgresql" else get_connection()
    cursor = conn.cursor() if scheme == "sqlite" else get_cursor(conn)
    if scheme == "postgresql":
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    id_type = "INTEGER PRIMARY KEY AUTOINCREMENT" if scheme == "sqlite" else "SERIAL PRIMARY KEY"
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS entries (
            id {id_type},
            app TEXT,
            title TEXT,
            text TEXT,
            timestamp INTEGER UNIQUE,
            embedding {embedding_sql_type},
            filename TEXT,
            ocr_data TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON entries (timestamp)")
    if scheme == "postgresql":
        # Ensure unique constraint on timestamp
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = 'entries'::regclass
                AND contype = 'u'
                AND conkey = ARRAY(
                    SELECT attnum FROM pg_attribute
                    WHERE attrelid = 'entries'::regclass
                    AND attname = 'timestamp'
                )
            );
        """)
        exists = cursor.fetchone()[0]
        if not exists:
            cursor.execute("ALTER TABLE entries ADD CONSTRAINT entries_timestamp_unique UNIQUE (timestamp);")
    conn.commit()
    conn.close()

def get_all_entries() -> List[Entry]:
    conn = get_connection()
    cursor = conn.cursor() if scheme == "sqlite" else get_cursor(conn)
    cursor.execute("SELECT id, app, title, text, timestamp, embedding, filename, ocr_data FROM entries ORDER BY timestamp DESC")
    results = cursor.fetchall()
    entries = []
    for row in results:
        embedding = deserialize_embedding(row["embedding"])
        entries.append(
            Entry(
                id=row["id"],
                app=row["app"],
                title=row["title"],
                text=row["text"],
                timestamp=row["timestamp"],
                embedding=embedding,
                filename=row["filename"],
                ocr_data=row["ocr_data"],
            )
        )
    conn.close()
    return entries

def get_timestamps() -> List[int]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp FROM entries ORDER BY timestamp DESC")
    timestamps = [row[0] for row in cursor.fetchall()]
    conn.close()
    return timestamps

def insert_entry(
    text: str, timestamp: int, embedding: np.ndarray, app: str, title: str, filename: str, ocr_data: str
) -> Optional[int]:
    serialized = serialize_embedding(embedding)
    conn = get_connection()
    cursor = get_cursor(conn)
    if scheme == "sqlite":
        cursor.execute(
            """INSERT OR IGNORE INTO entries (text, timestamp, embedding, app, title, filename, ocr_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (text, timestamp, serialized, app, title, filename, ocr_data),
        )
        if cursor.rowcount > 0:
            cursor.execute("SELECT id FROM entries WHERE timestamp = ?", (timestamp,))
            row = cursor.fetchone()
            last_id = row["id"]
        else:
            last_id = None
    elif scheme == "postgresql":
        cursor.execute(
            """INSERT INTO entries (text, timestamp, embedding, app, title, filename, ocr_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (timestamp) DO NOTHING
            RETURNING id""",
            (text, timestamp, serialized, app, title, filename, ocr_data),
        )
        row = cursor.fetchone()
        last_id = row["id"] if row else None
    conn.commit()
    conn.close()
    return last_id

def get_sorted_entries(query_embedding: np.ndarray, top_k: int = 100) -> List[Entry]:
    if scheme == "sqlite":
        entries = get_all_entries()
        if not entries:
            return []
        similarities = [cosine_similarity(query_embedding, e.embedding) for e in entries]
        indices = np.argsort(similarities)[::-1][:top_k]
        return [entries[i] for i in indices]
    elif scheme == "postgresql":
        serialized_query = serialize_embedding(query_embedding)
        conn = get_connection()
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT id, app, title, text, timestamp, embedding, filename, ocr_data FROM entries ORDER BY embedding <=> {param_placeholder} LIMIT {param_placeholder}",
            (serialized_query, top_k),
        )
        results = cursor.fetchall()
        entries = [
            Entry(
                id=row["id"],
                app=row["app"],
                title=row["title"],
                text=row["text"],
                timestamp=row["timestamp"],
                embedding=deserialize_embedding(row["embedding"]),
                filename=row["filename"],
                ocr_data=row["ocr_data"],
            )
            for row in results
        ]
        conn.close()
        return entries
    else:
        return []
