# Built on the device by the Supervisor; keys are never baked in (they are
# generated into /data/certs on first start).
#
# Reproducible: the base is pinned by tag and multi-arch digest, and
# requirements.txt pins every package with hashes (generated from
# requirements.in). Dependabot bumps both. No build.yaml, so the Supervisor
# passes no BUILD_FROM and builds exactly this base.
FROM python:3.15.1-alpine3.24@sha256:54448f67d7c31c39e5cdd3ea2d6382395ce142806c284425f8e897d78a0465b3

RUN apk add --no-cache openssl
WORKDIR /app
# DH parameters are not secret and take ~30 s to compute, so they are built in
# rather than generated on every first start.
RUN openssl dhparam -out /app/dhparams-default.pem 2048 2>/dev/null

COPY requirements.txt /app/
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt

COPY icomm_relay.py icomm_frames.py icomm_lan.py icomm_peers.py upstream-entrust-g2.pem gen-cert.sh run.sh /app/
RUN chmod +x /app/gen-cert.sh /app/run.sh

CMD ["/app/run.sh"]
