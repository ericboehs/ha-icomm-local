DOMAIN = "icomm_local"
POLL_SECONDS = 10
# The relay answers locally; this is only generous enough to ride a slow host.
TIMEOUT_SECONDS = 10

# The three mode names the appliance's own register accepts. Guest and vacation
# are separate register pairs that were never captured, so they are absent on
# purpose rather than guessed at.
MODES = ("electric", "hybrid", "heat_pump")
