# Built on the device by the Supervisor; keys are never baked in (they are
# generated into /data/certs on first start).
ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

RUN apk add --no-cache openssl
WORKDIR /app
# DH parameters are not secret and take ~30 s to compute, so they are built in
# rather than generated on every first start.
RUN openssl dhparam -out /app/dhparams-default.pem 2048 2>/dev/null

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY icomm_relay.py icomm_frames.py icomm_lan.py upstream-entrust-g2.pem gen-cert.sh run.sh /app/
RUN chmod +x /app/gen-cert.sh /app/run.sh

CMD ["/app/run.sh"]
