"""TCP port probe.

Config:
  type: port
  host: 127.0.0.1
  port: 3000
  timeout: 4
  level_if_down: crit
"""
import socket
import time

from .base import Probe, result, event


class PortProbe(Probe):
    def run(self):
        cfg = self.cfg
        host = cfg.get("host", "127.0.0.1")
        port = int(cfg["port"])
        timeout = cfg.get("timeout", 4)
        start = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                latency = round((time.time() - start) * 1000)
            return result("ok", "", {"port_%d_ms" % port: latency})
        except OSError as exc:
            level = cfg.get("level_if_down", "crit")
            return result(level, f"port {port} closed",
                          events=[event(level, f"Port {port} unreachable", str(exc))])
