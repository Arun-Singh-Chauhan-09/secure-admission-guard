# Python standard library only: nothing is pip-installed, so pip itself is removed.
# The image scan found 5 HIGH CVEs in libraries that pip bundles (e.g. msgpack); the
# webhook never runs pip, so removing it is safer than ignoring the findings.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip
WORKDIR /app
COPY guard ./guard
# Fixed numeric UID so Kubernetes runAsNonRoot can verify it.
USER 65532:65532
EXPOSE 8443 8080
ENTRYPOINT ["python", "-m", "guard.server"]
