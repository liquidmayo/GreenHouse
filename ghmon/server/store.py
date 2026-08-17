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
CALLS_KEPT = 300        # recent radio calls kept in memory for the call feed
SAMPLE_RETENTION_H = 48
EVENT_RETENTION_H = 24 * 7
TREND_RETENTION_D = 30      # 5-minute rollups of featured metrics kept this long
TREND_BUCKET_S = 300

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
CREATE TABLE IF NOT EXISTS trends (
    machine TEXT NOT NULL,
    component TEXT NOT NULL,
    metric TEXT NOT NULL,
    bucket INTEGER NOT NULL,     -- epoch seconds floored to TREND_BUCKET_S
    total REAL NOT NULL,
    n INTEGER NOT NULL,
    peak REAL NOT NULL,
    PRIMARY KEY (machine, component, metric, bucket)
);
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
        self.recent_calls = collections.deque(maxlen=CALLS_KEPT)
        self._last_prune = 0
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        self._backfill_trends()

    def _backfill_trends(self):
        """One-time: if the trends table is empty but raw samples exist,
        roll the raw history up so graphs aren't blank after the upgrade.
        Uses a fixed key list because raw samples don't record which
        metrics were 'featured'."""
        keys = ("listeners", "listener_count", "followers", "viewers")
        try:
            with self._conn() as conn:
                if conn.execute("SELECT 1 FROM trends LIMIT 1").fetchone():
                    return
                rows = conn.execute(
                    "SELECT machine, component, ts, metrics FROM samples").fetchall()
                agg = {}
                for r in rows:
                    try:
                        metrics = json.loads(r["metrics"] or "{}")
                    except ValueError:
                        continue
                    bucket = int(r["ts"] // TREND_BUCKET_S * TREND_BUCKET_S)
                    for k in keys:
                        v = metrics.get(k)
                        if isinstance(v, (int, float)):
                            slot = agg.setdefault((r["machine"], r["component"], k, bucket),
                                                  [0.0, 0, float("-inf")])
                            slot[0] += v; slot[1] += 1; slot[2] = max(slot[2], v)
                if agg:
                    conn.executemany(
                        "INSERT OR REPLACE INTO trends "
                        "(machine, component, metric, bucket, total, n, peak) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(m, c, k, b, t, n, p) for (m, c, k, b), (t, n, p) in agg.items()])
                    log.info("backfilled %d trend buckets from %d raw samples",
                             len(agg), len(rows))
        except Exception:
            log.exception("trend backfill failed")

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def ingest(self, payload):
        machine = payload.get("machine") or "unknown"
        now = time.time()
        rows = []
        trend_rows = []
        new_events = []
        for comp in payload.get("components", []):
            cid = comp.get("id", "?")
            rows.append((machine, cid, payload.get("ts", now), comp.get("status", "unknown"),
                         json.dumps(comp.get("metrics", {}))))
            # long-term rollup for the component's spotlight metrics
            bucket = int(payload.get("ts", now) // TREND_BUCKET_S * TREND_BUCKET_S)
            for key in list(comp.get("featured", [])) + list(comp.get("featured_card", [])):
                val = comp.get("metrics", {}).get(key)
                if isinstance(val, (int, float)):
                    trend_rows.append((machine, cid, key, bucket, float(val)))
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
                conn.executemany(
                    "INSERT INTO trends (machine, component, metric, bucket, total, n, peak) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(machine, component, metric, bucket) DO UPDATE SET "
                    "total = total + excluded.total, n = n + 1, "
                    "peak = MAX(peak, excluded.peak)",
                    [(m, c, k, b, v, v) for (m, c, k, b, v) in trend_rows])
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
                conn.execute("DELETE FROM trends WHERE bucket < ?",
                             (now - TREND_RETENTION_D * 86400,))
        except Exception:
            log.exception("prune failed")

    def trend(self, machine, component, metric, hours=24):
        """5-minute averaged history of one metric: [{ts, avg, peak}, ...]."""
        since = time.time() - hours * 3600
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT bucket, total, n, peak FROM trends "
                "WHERE machine = ? AND component = ? AND metric = ? AND bucket > ? "
                "ORDER BY bucket", (machine, component, metric, since)).fetchall()
        return [{"ts": r["bucket"], "avg": round(r["total"] / r["n"], 1),
                 "peak": r["peak"]} for r in rows]

    def trend_merged(self, metric, hours=24):
        """Same metric summed across all machines/components per bucket
        (e.g. total rdio listeners local + remote)."""
        since = time.time() - hours * 3600
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT bucket, SUM(total * 1.0 / n) AS avg, SUM(peak) AS peak "
                "FROM trends WHERE metric = ? AND bucket > ? "
                "GROUP BY bucket ORDER BY bucket", (metric, since)).fetchall()
        return [{"ts": r["bucket"], "avg": round(r["avg"], 1), "peak": r["peak"]}
                for r in rows]

    def note_call(self, payload):
        """Record one radio call pushed by the SDRTrunk webhook broadcaster."""
        now = time.time()
        ts = payload.get("timestampSeconds")
        if not isinstance(ts, (int, float)) or ts <= 0:
            ts = now
        call = {
            "ts": ts,
            "system": payload.get("systemLabel") or payload.get("system") or "",
            "talkgroup": payload.get("talkgroupLabel")
                         or str(payload.get("talkgroup") or ""),
            "radio": payload.get("talkerAlias")
                     or str(payload.get("radioId") or ""),
            "duration_s": payload.get("durationSeconds"),
            "frequency": payload.get("frequency"),
        }
        with self.lock:
            self.recent_calls.appendleft(call)

    def call_stats(self):
        now = time.time()
        with self.lock:
            calls = list(self.recent_calls)
        last_min = sum(1 for c in calls if now - c["ts"] <= 60)
        last_hour = sum(1 for c in calls if now - c["ts"] <= 3600)
        return {
            "last_min": last_min,
            "last_hour": last_hour,
            "last_call_age_s": round(now - calls[0]["ts"]) if calls else None,
            "recent": calls[:15],
        }

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
                "events": list(self.recent_events)[:60],
                "calls": self.call_stats()}

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
