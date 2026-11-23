FROM python:3.15-alpine

# Unbuffered so log lines reach `docker logs` immediately.
ENV PYTHONUNBUFFERED=1

COPY wol_relay.py /app/wol_relay.py

# Overridden by `user:` in compose; this is just a non-root default.
USER 65534:65534

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python3", "-I", "/app/wol_relay.py", "--healthcheck"]

ENTRYPOINT ["python3", "-I", "/app/wol_relay.py"]
