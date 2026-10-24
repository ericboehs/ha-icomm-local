"""Docker HEALTHCHECK: is the relay's event loop answering?

Runs inside the add-on container, which shares the host network, so the
loopback-only state API is in reach. The Supervisor restarts the add-on when
Docker reports it unhealthy, if the add-on's Watchdog toggle is on. Stdlib
only: the image has no pip and needs nothing extra for this.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

OPTIONS = "/data/options.json"


def state_port(options_path: str = OPTIONS) -> int:
    """The port run.sh gives the relay: env, then the add-on option, then 8124."""
    if os.environ.get("STATE_PORT"):
        return int(os.environ["STATE_PORT"])
    try:
        with open(options_path) as handle:
            port = json.load(handle).get("state_port")
    except (OSError, ValueError, AttributeError):
        port = None
    return int(port) if port else 8124


def healthy(port: int, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=timeout) as reply:
            return reply.status == 200 and json.load(reply).get("ok") is True
    except (OSError, ValueError):
        return False


def main() -> int:
    return 0 if healthy(state_port()) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
