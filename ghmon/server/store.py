"""State store for the master dashboard.

Latest machine payloads and short status strips live in memory; samples and
events are persisted to SQLite for history queries, pruned on a rolling window.
"""
import collections
import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger("ghmon.store")

STRIP_LEN = 60          # status points kept per component for the uptime strip
EVENTS_KEPT = 200       # recent events kept in memory for the feed
SAMPLE_RETENTION_H = 48
EVENT_RETENTION_H = 24 * 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    component TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    metrics TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples (machine, component, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    component TEXT NOT NULL,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    label TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.lock = threading.Lock()
        self.latest = {}   # machine -> {payload, received_at}
        self.strips = collections.defaultdict(
            lambda: collections.deque(maxlen=STRIP_LEN))  # (machine, comp) -> deque
        self.recent_events = collections.deque(maxlen=EVENTS_KEPT)
        self._event_keys = collections.deque(maxlen=EVENTS_KEPT)
        self._last_prune = 0
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def ingest(self, payload):
        machine = payload.get("machine") or "unknown"
        now = time.time()
        rows = []
        new_events = []
        for comp in payload.get("components", []):
            cid = comp.get("id", "?")
            rows.append((machine, cid, payload.get("ts", now), comp.get("status", "unknown"),
                         json.dumps(comp.get("metrics", {}))))
            self.strips[(machine, cid)].append(
                {"ts": payload.get("ts", now), "s": comp.get("status", "unknown")})
            for ev in comp.get("events", []):
                # de-dupe repeats (same component + label + message)
                key = (machine, cid, ev.get("label"), ev.get("message"))
                if key in self._event_keys:
                    continue
                self._event_keys.append(key)
                item = {
                    "machine": machine, "component": cid,
                    "component_label": comp.get("label", cid),
                    "ts": ev.get("ts", now), "level": ev.get("level", "warn"),
                    "label": ev.get("label", ""), "message": ev.get("message", ""),
                }
                new_events.append(item)
                self.recent_events.appendleft(item)

        with self.lock:
            self.latest[machine] = {"payload": payload, "received_at": now}
            with self._conn() as conn:
                conn.executemany(
                    "INSERT INTO samples (machine, component, ts, status, metrics) "
                    "VALUES (?, ?, ?, ?, ?)", rows)
                conn.executemany(
                    "INSERT INTO events (machine, component, ts, level, label, message) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(e["machine"], e["component"], e["ts"], e["level"],
                      e["label"], e["message"]) for e in new_events])
        self._maybe_prune(now)

    def _maybe_prune(self, now):
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            with self.lock, self._conn() as conn:
                conn.execute("DELETE FROM samples WHERE ts < ?",
                             (now - SAMPLE_RETENTION_H * 3600,))
                conn.execute("DELETE FROM events WHERE ts < ?",
                             (now - EVENT_RETENTION_H * 3600,))
        except Exception:
            log.exception("prune failed")

    def state(self):
        now = time.time()
        machines = {}
        with self.lock:
            snapshot = dict(self.latest)
        for machine, entry in snapshot.items():
            payload = entry["payload"]
            age = now - entry["received_at"]
            interval = payload.get("interval", 15)
            offline = age > max(60, interval * 4)
            comps = []
            for comp in payload.get("components", []):
                comp = dict(comp)
                comp.pop("events", None)
                comp["strip"] = list(self.strips[(machine, comp["id"])])
                if offline:
                    comp["status"] = "unknown"
                    comp["summary"] = "agent offline — last data %ds ago" % round(age)
                comps.append(comp)
            machines[machine] = {
                "received_at": entry["received_at"],
                "agent_age_s": round(age),
                "offline": offline,
                "agent_version": payload.get("agent_version"),
                "system": payload.get("system", {}),
                "components": comps,
            }
        return {"ts": now, "machines": machines,
                "events": list(self.recent_events)[:60]}

    def history(self, machine, component, hours=6):
        since = time.time() - hours * 3600
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, status, metrics FROM samples "
                "WHERE machine = ? AND component = ? AND ts > ? ORDER BY ts",
                (machine, component, since)).fetchall()
        return [{"ts": r["ts"], "status": r["status"],
                 "metrics": json.loads(r["metrics"] or "{}")} for r in rows]

    def events(self, limit=100):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT machine, component, ts, level, label, message FROM events "
                "ORDER BY ts DESC LIMIT ?", (min(limit, 500),)).fetchall()
        return [dict(r) for r in rows]
