# GreenHouse Monitor

Generic infrastructure monitoring dashboard: processes, TCP ports, JSON
health endpoints, log files, file write activity, and CPU/memory/disk —
across one machine or many.

One codebase, two roles:

- **Master** (`start.bat`) — runs the web dashboard on port **8090** and
  collects data on this machine.
- **Companion agent** (`start-agent.bat`) — runs on any other machine,
  collects the same kinds of data locally, and reports back to the master
  over HTTP (buffers while the master is unreachable).

## Quick start

1. Edit `monitors.yml` — add a component per service you want watched
   (the file contains a full probe reference and an example).
2. Run `start.bat` (requires Python 3.10+; a virtualenv is created on
   first run).
3. Open <http://localhost:8090/>.

## Deploying a companion agent to another machine

1. Copy this folder (minus `.venv` and `data`) to the remote machine.
2. Replace `monitors.yml` with a copy of `monitors.agent.example.yml`,
   set `master_url`, `api_key`, and its `components`.
3. Run `start-agent.bat`. The machine appears on the dashboard within ~15s.

## Probe types

| Type | What it does |
|---|---|
| `process` | Process running (name and/or command-line match), memory usage |
| `port` | TCP port accepting connections, latency |
| `http_json` | Poll a JSON endpoint; `checks` (equals/min/max/max_age), `collect` metrics, `rates` for counters |
| `logtail` | Incremental log tail with regex patterns + heartbeat-silence detection; handles rotation |
| `file_activity` | Alert when a file (or newest glob match) stops being written |
| `rdio_admin` | rdio-scanner admin API: listener count, call-ingest rate, API errors |

## Notes

- `featured: [metric_key]` on a component spotlights that metric in the
  hero strip at the top of the dashboard and enlarges it on the card.
- `brand:` in `monitors.yml` sets the dashboard title text.
- History is kept in `data\monitor.db` (48h samples, 7d events, auto-pruned).
- The dashboard has no login and the ingest API is protected only by the
  shared `api_key` — keep it on a trusted LAN; change the default key.
