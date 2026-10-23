#!/usr/bin/env bash
# Generate the TLS leaf the iCOMM module accepts: RSA-2048, signed SHA-256,
# named *.aylanetworks.com. The module never validates the chain -- measured
# across a day of handshakes -- so a self-signed leaf is enough. An ECDSA/P-384
# leaf DEADLOCKS the handshake silently for 30 s at a time, which reads exactly
# like a trust rejection; that trap cost an evening, so it is stated here plainly.
#
#   ./gen-cert.sh            # writes certs/aylanetworks.com.{crt,key}, dhparams.pem
#
# The 2048-bit DH parameters take a few seconds; the module refuses nothing here,
# but 2048 is the measured size that works (4096 also works, 1024 does not).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p certs
[ -f certs/aylanetworks.com.key ] || openssl genrsa -out certs/aylanetworks.com.key 2048
[ -f certs/aylanetworks.com.crt ] || openssl req -new -x509 -sha256 -days 825 \
  -key certs/aylanetworks.com.key \
  -subj "/CN=*.aylanetworks.com/O=icomm_local relay" \
  -out certs/aylanetworks.com.crt
[ -f certs/dhparams.pem ] || openssl dhparam -out certs/dhparams.pem 2048 2>/dev/null

echo "wrote certs/aylanetworks.com.crt (leaf), .key, dhparams.pem"
echo "keep the key out of git: certs/ is in .gitignore"
