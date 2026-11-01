# Python standard library only: no pip install, nothing to patch but the base image.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY guard ./guard
# Fixed numeric UID so Kubernetes runAsNonRoot can verify it.
USER 65532:65532
EXPOSE 8443 8080
ENTRYPOINT ["python", "-m", "guard.server"]
