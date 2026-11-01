# Python standard library only: nothing is pip-installed, so pip itself is removed.
# The image scan found 5 HIGH CVEs in libraries that pip bundles (e.g. msgpack); the
# webhook never runs pip, so removing it is safer than ignoring the findings.
#
# The base image is pinned by digest, the same rule this webhook enforces on everyone
# else: a tag like 3.13-slim can be moved, a digest cannot. Dependabot opens a PR when a
# new digest is published, and CI rejects a FROM line without one.
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip
WORKDIR /app
# Owned by root and read-only (0555): the app user can run the code but never change it.
COPY --chmod=0555 guard ./guard
# Fixed numeric UID so Kubernetes runAsNonRoot can verify it.
USER 65532:65532
EXPOSE 8443 8080
ENTRYPOINT ["python", "-m", "guard.server"]
