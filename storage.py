import json
import os
import sqlite3
from pathlib import Path

class Store:
    def __init__(self):
        url = os.getenv('DATABASE_URL', '').strip()
        path = os.getenv('GAME_STATE_PATH', '').strip()
        self.connection = None
        self.bind = '?'
        if url:
            import psycopg
            self.connection = psycopg.connect(url, autocommit=True, connect_timeout=8)
            self.bind = '%s'
        elif path:
            Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if self.connection:
            self.connection.execute('CREATE TABLE IF NOT EXISTS kammel_state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')

    def load(self):
        if not self.connection:
            return {}
        row = self.connection.execute('SELECT payload FROM kammel_state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, payload):
        if self.connection:
            self.connection.execute(f'INSERT INTO kammel_state (id,payload) VALUES (1,{self.bind}) ON CONFLICT (id) DO UPDATE SET payload=excluded.payload', (json.dumps(payload, ensure_ascii=False),))
