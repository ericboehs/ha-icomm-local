#!/bin/sh
# Container entrypoint: bootstrap keys, take options from the add-on, then run.
#
# The TLS leaf must be generated on this host and kept out of the image, so it
# goes in $DATA_DIR/certs (a docker volume, or /data for an add-on). Regenerate
# freely: the module does not validate the certificate, not even its name --
# measured (CN=garbage.example with no SAN at all was accepted).
set -eu
DATA_DIR="${DATA_DIR:-/data}"
CERT_DIR="${CERT_DIR:-$DATA_DIR/certs}"

mkdir -p "$CERT_DIR"

# Reuse the build-time DH params first (public, ~30 s to compute) so the leaf
# generation below does not recompute them on a fresh install.
if [ ! -f "$CERT_DIR/dhparams.pem" ] && [ -f ./dhparams-default.pem ]; then
  cp ./dhparams-default.pem "$CERT_DIR/dhparams.pem"
fi

if [ ! -f "$CERT_DIR/aylanetworks.com.crt" ]; then
  echo "no key at $CERT_DIR, generating"
  CERT_DIR="$CERT_DIR" ./gen-cert.sh
fi
# Reuse the build-time DH params if this host has none yet (they are public).
if [ ! -f "$CERT_DIR/dhparams.pem" ] && [ -f ./dhparams-default.pem ]; then
  cp ./dhparams-default.pem "$CERT_DIR/dhparams.pem"
fi
export CERT_DIR
# The write queue must outlive the container: /data is what survives a restart or an
# add-on update. Keys and queue live side by side, and neither is in the image.
export QUEUE_FILE="${QUEUE_FILE:-$DATA_DIR/pending.json}"

# Add-on options arrive as JSON at /data/options.json; map to the relay's env.
if [ -f "$DATA_DIR/options.json" ]; then
  eval "$(python3 - "$DATA_DIR/options.json" <<'PY'
import json, shlex, sys
try:
    opts = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    opts = {}
if opts.get("lan_mode") is False:
    print("export LAN_MODE=0")
if opts.get("reporting_mask_renew") is True:
    print("export REPORTING_MASK_RENEW=1")
if opts.get("offline_fallback") is False:
    print("export OFFLINE_FALLBACK=0")
if opts.get("dns_server") is True:
    print("export DNS_SERVER=1")
for key, env in (("listen_port", "LISTEN_PORT"), ("state_port", "STATE_PORT"),
                 ("upstream", "UPSTREAM"), ("device_dsn", "DEVICE_DSN"),
                 ("lan_port", "LAN_PORT"), ("module_ip", "MODULE_IP"),
                 ("relay_ip", "RELAY_IP"), ("trusted_peers", "TRUSTED_PEERS"),
                 ("firmware_updates", "FIRMWARE_UPDATES"),
                 ("dns_upstream", "DNS_UPSTREAM")):
    if opts.get(key) not in (None, ""):
        print(f"export {env}={shlex.quote(str(opts[key]))}")
PY
)"
fi

echo "icomm_local relay: listen=${LISTEN_PORT:-8444} state=${STATE_PORT:-8124} upstream=${UPSTREAM:-default}"
exec python3 icomm_relay.py
