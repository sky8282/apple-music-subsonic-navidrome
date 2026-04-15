import sqlite3
import time
import json
import random
import uuid
from typing import List, Dict, Any

class Database:
    def __init__(self, db_path="apple_music_bridge.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._migrate_legacy_ids() 

    def _init_db(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS playlists (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created INTEGER,
                    updated INTEGER,
                    song_count INTEGER DEFAULT 0,
                    duration INTEGER DEFAULT 0
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    id TEXT PRIMARY KEY,
                    playlist_id TEXT NOT NULL,
                    song_id TEXT NOT NULL,
                    song_json TEXT NOT NULL,
                    sort_order INTEGER,
                    FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
                )
            """)

    def _migrate_legacy_ids(self):
        try:
            cursor = self.conn.execute("SELECT id FROM playlists")
            for row in cursor.fetchall():
                old_id = str(row["id"]) 
                if "-" not in old_id: 
                    new_id = str(uuid.uuid5(uuid.NAMESPACE_OID, old_id))
                    with self.conn:
                        self.conn.execute("UPDATE playlists SET id = ? WHERE id = ?", (new_id, row["id"]))
                        self.conn.execute("UPDATE playlist_tracks SET playlist_id = ? WHERE playlist_id = ?", (new_id, row["id"]))
        except Exception as e: 
            pass
            
        try:
            cursor2 = self.conn.execute("SELECT id FROM playlist_tracks")
            for row in cursor2.fetchall():
                old_id = str(row["id"])
                if "-" not in old_id:
                    new_id = str(uuid.uuid5(uuid.NAMESPACE_OID, old_id))
                    with self.conn:
                        self.conn.execute("UPDATE playlist_tracks SET id = ? WHERE id = ?", (new_id, row["id"]))
        except Exception as e: 
            pass
            
    def get_playlists(self) -> List[Dict]:
        cursor = self.conn.execute("SELECT * FROM playlists ORDER BY updated DESC")
        return [dict(row) for row in cursor.fetchall()]

    def get_playlist(self, playlist_id: str) -> Dict:
        cursor = self.conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def create_playlist(self, name: str, owner: str = "admin") -> str:
        playlist_id = str(uuid.uuid4())
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                "INSERT INTO playlists (id, name, owner, created, updated) VALUES (?, ?, ?, ?, ?)",
                (playlist_id, name, owner, now, now)
            )
        return playlist_id

    def delete_playlist(self, playlist_id: str):
        with self.conn:
            self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            self.conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))

    def update_playlist_name(self, playlist_id: str, name: str):
        with self.conn:
            self.conn.execute("UPDATE playlists SET name = ?, updated = ? WHERE id = ?", 
                              (name, int(time.time()), playlist_id))

    def get_playlist_tracks(self, playlist_id: str) -> List[Dict]:
        cursor = self.conn.execute(
            "SELECT song_json FROM playlist_tracks WHERE playlist_id = ? ORDER BY sort_order ASC", 
            (playlist_id,)
        )
        return [json.loads(row["song_json"]) for row in cursor.fetchall()]

    def add_tracks_to_playlist(self, playlist_id: str, tracks: List[Dict]):
        if not tracks: return
        now = int(time.time())
        cursor = self.conn.execute("SELECT MAX(sort_order) as max_order FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        max_order = (cursor.fetchone()["max_order"] or 0)
        total_duration_added = 0
        with self.conn:
            for track in tracks:
                max_order += 1
                track_id = str(uuid.uuid4())
                song_id = str(track.get("id", ""))
                total_duration_added += int(track.get("duration", 0))
                self.conn.execute(
                    "INSERT INTO playlist_tracks (id, playlist_id, song_id, song_json, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (track_id, playlist_id, song_id, json.dumps(track), max_order)
                )
            self.conn.execute("""
                UPDATE playlists 
                SET song_count = song_count + ?, duration = duration + ?, updated = ? 
                WHERE id = ?
            """, (len(tracks), total_duration_added, now, playlist_id))

    def remove_track_from_playlist(self, playlist_id: str, song_index: int):
        cursor = self.conn.execute(
            "SELECT id, song_json FROM playlist_tracks WHERE playlist_id = ? ORDER BY sort_order ASC LIMIT 1 OFFSET ?", 
            (playlist_id, song_index)
        )
        row = cursor.fetchone()
        if row:
            track_db_id = row["id"]
            track_data = json.loads(row["song_json"])
            duration_to_remove = int(track_data.get("duration", 0))
            with self.conn:
                self.conn.execute("DELETE FROM playlist_tracks WHERE id = ?", (track_db_id,))
                self.conn.execute("""
                    UPDATE playlists 
                    SET song_count = MAX(0, song_count - 1), duration = MAX(0, duration - ?), updated = ? 
                    WHERE id = ?
                """, (duration_to_remove, int(time.time()), playlist_id))

db = Database()