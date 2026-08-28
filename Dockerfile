# a-doc container image: runs both the web UI (`adoc serve`) and the
# scheduled jobs (`adoc ingest`, `adoc review`, `adoc backup`) as ECS
# Fargate tasks from this one image (see deploy/cfn/ecs.yaml) - only the
# command differs per task definition. Replaces deploy/install.sh's
# EC2 boot-time provisioning entirely; see docs/adr/0006-fargate-efs.md.
FROM python:3.12-slim

# Official standalone uv binary (no installer script, no pip bootstrap).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# poppler-utils: pdftoppm page rendering (ingest.archive). git: the data
# repo (GitPython shells out to the git binary) + `adoc backup`'s git
# bundle. rclone: the Dropbox inbox pull (see run-ingest.sh). curl: IMDS/
# health probes and debugging inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        git \
        rclone \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root, fixed uid/gid 1000 - matches the EFS AccessPoint's PosixUser
# (deploy/cfn/ecs.yaml) so files this process writes under /data are
# readable/writable through the access point without a chown step.
RUN groupadd --gid 1000 adoc \
    && useradd --uid 1000 --gid adoc --create-home --shell /usr/sbin/nologin adoc

ENV ADOC_DATA_DIR=/data/a-doc-data \
    HOME=/home/adoc \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Dependency manifests first so dependency layers cache independently of
# application code changes.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

# `Settings.models_file` defaults to the relative path "models.yaml",
# resolved against the process's working directory (/app here) - every
# `adoc` subcommand that constructs `Settings()` needs this file present,
# not just source code.
COPY models.yaml ./

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY deploy/container/run-ingest.sh /usr/local/bin/run-ingest.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/run-ingest.sh

# /data is the EFS mount point in production (deploy/cfn/ecs.yaml); create
# it here too so a bare `docker run` without a volume still has somewhere
# writable to `adoc init` into.
RUN mkdir -p /data \
    && chown -R adoc:adoc /app /data /home/adoc

USER adoc

EXPOSE 8080

# --- HPO index (ADR 0031's phenotype profile) -------------------------------
# The compact label/synonym index the phenotype matcher needs, built from the
# published ontology at image-build time. Baked in rather than downloaded at
# start: a deploy stays reproducible and a running container has no network
# dependency. ~3MB, down from hp.json's 22MB, because matching a patient's
# words to a term needs labels and synonyms, not axioms or edges.
#
# One RUN so the 22MB source never lands in a layer of its own.
COPY scripts/build_hpo_index.py /tmp/build_hpo_index.py
RUN curl -sSL -o /tmp/hp.json \
      https://github.com/obophenotype/human-phenotype-ontology/releases/latest/download/hp.json \
    && python /tmp/build_hpo_index.py /tmp/hp.json /opt/hpo-index.json \
    && rm -f /tmp/hp.json /tmp/build_hpo_index.py

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["adoc", "serve", "--host", "0.0.0.0", "--port", "8080"]
