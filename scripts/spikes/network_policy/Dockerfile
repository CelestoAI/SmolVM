# Disposable engine test environment; NOT a production proxy image.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca
WORKDIR /spike
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --from=policy engine.py /spike/candidate.py
COPY tests ./tests
# Tests only need loopback sockets and disposable certificate files in /tmp.
USER 65534:65534
ENV PYTHONDONTWRITEBYTECODE=1
CMD [".venv/bin/pytest", "-q", "--disable-warnings", "-p", "no:cacheprovider"]
