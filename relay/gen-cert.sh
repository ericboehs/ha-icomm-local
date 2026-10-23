#!/usr/bin/env bash
# Generate the TLS leaf the iCOMM module accepts: RSA-2048, signed SHA-256.
#
# The module validates nothing about the certificate -- not the chain, not the
# name, not the SAN. Measured: a leaf with CN=garbage.example and no SAN at all
# was accepted on the first handshake, telemetry flowed, and a queued command
# was even retrieved through it. The aylanetworks.com name below is kept only as
# a harmless convention (it makes a packet capture look right); changing it costs
# nothing. An ECDSA/P-384 leaf, by contrast, DEADLOCKS the handshake silently for
# 30 s at a time, which reads exactly like a trust rejection -- that trap cost an
# evening, so it is stated here plainly.
#
#   ./gen-cert.sh            # writes certs/aylanetworks.com.{crt,key}, dhparams.pem
#
# The 2048-bit DH parameters take a few seconds; the module refuses nothing here,
# but 2048 is the measured size that works (4096 also works, 1024 does not).
set -euo pipefail
cd "$(dirname "$0")"

# Keys live wherever CERT_DIR points: ./certs for a manual run, /data/certs
# inside the add-on so they survive restarts and never ship in the image.
CERT_DIR="${CERT_DIR:-certs}"
mkdir -p "$CERT_DIR"
[ -f $CERT_DIR/aylanetworks.com.key ] || openssl genrsa -out $CERT_DIR/aylanetworks.com.key 2048
[ -f $CERT_DIR/aylanetworks.com.crt ] || openssl req -new -x509 -sha256 -days 825 \
  -key $CERT_DIR/aylanetworks.com.key \
  -subj "/CN=*.aylanetworks.com/O=icomm_local relay" \
  -out $CERT_DIR/aylanetworks.com.crt
[ -f $CERT_DIR/dhparams.pem ] || openssl dhparam -out $CERT_DIR/dhparams.pem 2048 2>/dev/null

echo "wrote $CERT_DIR/aylanetworks.com.crt (leaf), .key, dhparams.pem"
echo "keep the key out of images and git: keys stay in $CERT_DIR"
