# Sandbox image for the bug-fix agent. Built once with network access;
# containers from it run with --network none.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "pytest>=8" \
 && useradd --create-home --uid 1000 agent \
 && git config --system --add safe.directory '*'

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider"
USER agent
WORKDIR /repo
CMD ["sleep", "infinity"]
