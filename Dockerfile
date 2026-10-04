# Switchboard AI in a container: the server plus the Claude Code and
# Antigravity CLIs it drives. Sign-ins live in volumes (see docker-compose.yml),
# so they survive rebuilds.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash app
USER app
WORKDIR /home/app/switchboard
ENV PATH=/home/app/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

# Official installers; both put their binary in ~/.local/bin.
RUN curl -fsSL https://claude.ai/install.sh | bash
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    || echo "Antigravity CLI install failed; Claude still works."

COPY --chown=app:app pyproject.toml README.md LICENSE ./
COPY --chown=app:app switchboard_ai ./switchboard_ai
RUN python -m venv .venv && .venv/bin/pip install --no-cache-dir ".[api]"

# Keep all of Claude Code's state (login + settings) in one folder = one volume.
ENV CLAUDE_CONFIG_DIR=/home/app/.claude \
    API_HOST=0.0.0.0 \
    API_PORT=8000
RUN mkdir -p /home/app/.claude /home/app/.gemini

EXPOSE 8000
CMD [".venv/bin/python", "-m", "switchboard_ai", "server"]
