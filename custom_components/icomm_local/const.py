DOMAIN = "icomm_local"
POLL_SECONDS = 10
# The relay answers locally; this is only generous enough to ride a slow host.
TIMEOUT_SECONDS = 10

# The mode names the relay can write and decode. Vacation was captured from the
# app (`01 02` for "1 day"); guest never has been, so it is absent on purpose
# rather than guessed at.
MODES = ("electric", "hybrid", "heat_pump", "vacation")
# Vacation's day count lives in the mode register's first byte (captured from the
# app as 1 and 3 days). Whether the controller counts it down is not yet observed.
VACATION_DEFAULT_DAYS = 1
MAX_VACATION_DAYS = 99


def has_telemetry(data: dict | None) -> bool:
    """Tank entities stay available while the relay answers and has *ever* decoded
    a report. Live finding: tying availability to `fresh` made an idle tank go
    unavailable after 10 quiet minutes, and Home Assistant then silently drops
    service calls to an unavailable entity -- writes vanished with only a log line.
    Staleness is a warning (attribute + Repairs issue), not unavailability."""
    data = data or {}
    return bool(data.get("online")) or data.get("setpoint") is not None


def is_stale(data: dict | None) -> bool:
    """Reachable relay, known tank, but no fresh report: warn, do not hide."""
    data = data or {}
    return has_telemetry(data) and not data.get("fresh")
