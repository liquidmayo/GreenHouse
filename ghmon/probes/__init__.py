"""Probe registry. Each probe type maps to a class that produces a ProbeResult."""
from .http_json import HttpJsonProbe
from .port_check import PortProbe
from .process_check import ProcessProbe
from .logtail import LogTailProbe
from .file_activity import FileActivityProbe
from .rdio_admin import RdioAdminProbe

PROBE_TYPES = {
    "http_json": HttpJsonProbe,
    "port": PortProbe,
    "process": ProcessProbe,
    "logtail": LogTailProbe,
    "file_activity": FileActivityProbe,
    "rdio_admin": RdioAdminProbe,
}


def build_probe(cfg):
    ptype = cfg.get("type")
    cls = PROBE_TYPES.get(ptype)
    if cls is None:
        raise ValueError(f"Unknown probe type: {ptype!r}")
    return cls(cfg)
