# LIRICAL sidecar — a non-LLM differential engine (ADR 0029).
#
# Separate image from the application on purpose: LIRICAL is a Java program,
# and the app image is `python:3.12-slim`. Putting a JRE into the app image
# would add ~200MB and a second runtime to every web task, for a capability
# only the deep review uses, a few times a day, for about a second.
#
# The data is baked in rather than downloaded at start, so a deploy is
# reproducible and a running container has no network dependency.

FROM eclipse-temurin:21-jre-noble

ARG LIRICAL_VERSION=2.4.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

RUN curl -sSL -o lirical.zip \
      "https://github.com/TheJacksonLaboratory/LIRICAL/releases/download/v${LIRICAL_VERSION}/lirical-cli-${LIRICAL_VERSION}-distribution.zip" \
    && unzip -q lirical.zip \
    && rm lirical.zip \
    && ln -s "/opt/lirical-cli-${LIRICAL_VERSION}/lirical-cli-${LIRICAL_VERSION}.jar" /opt/lirical.jar

ENV LIRICAL_DATA=/opt/liricaldata

# --- data download and its two required fixes, in ONE layer -----------------
#
# Deliberately a single RUN. Docker layers are additive, so downloading in one
# layer and truncating in the next leaves the full 347MB in the lower layer
# and saves nothing: measured, that mistake produced a 1.18GB image. The
# intermediate bytes must never be committed to a layer at all.
#
# Fix 1 - HGNC now ships two NCBI gene IDs under two symbols each, and
# LIRICAL 2.4.1 loads that file into a map with no merge function:
#   IllegalStateException: Duplicate key NCBIGene:100874204
#     (attempted merging values ALDH1L1-AS1 and SLC41A3-AS1)
# It aborts at bootstrap, before any analysis. Both collisions are
# non-coding-RNA/pseudogene antisense entries participating in no HPO disease
# annotation, so keeping the first row per ID is safe. The rule is generic
# rather than a hardcoded pair, because a later HGNC release collides
# elsewhere.
#
# Fix 2 - the transcript databases are 255MB of the download and are used only
# for VCF/variant analysis, which this sidecar never does. LIRICAL's data
# resolver checks only that they EXIST, so truncating them to zero bytes
# satisfies it; phenotype-only mode never deserializes them and the ranking is
# byte-identical. That is an assumption about lazy loading, which is why the
# smoke test below is not optional.
COPY dedupe_hgnc.py /opt/dedupe_hgnc.py
RUN java -jar /opt/lirical.jar download -d "$LIRICAL_DATA" \
    && python3 /opt/dedupe_hgnc.py "$LIRICAL_DATA/hgnc_complete_set.txt" \
    && truncate -s 0 "$LIRICAL_DATA"/*.ser \
    && du -sh "$LIRICAL_DATA"

# --- build-time smoke test --------------------------------------------------
# A fixed synthetic phenotype (arachnodactyly + aortic root aneurysm +
# bicuspid aortic valve) must still rank Loeys-Dietz/Marfan disorders at the
# top. This proves the jar runs, the data resolves, the HGNC fix held, and the
# truncation assumption still holds — all before the image is ever pushed.
RUN java -jar /opt/lirical.jar prioritize -d "$LIRICAL_DATA" \
      -p HP:0001166,HP:0002616,HP:0001647 -o /tmp/smoke -x smoke -f tsv \
    && grep -qi "loeys-dietz" /tmp/smoke/smoke.tsv \
    && echo "lirical smoke test passed" \
    && rm -rf /tmp/smoke

WORKDIR /work
ENTRYPOINT ["java", "-jar", "/opt/lirical.jar"]
CMD ["--help"]
