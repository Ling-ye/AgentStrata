FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS codex
RUN npm install --prefix /opt/tools @openai/codex@0.166.0
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /opt/tools /opt/tools
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates bubblewrap ripgrep && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 application && install -d -o application -g application -m 700 /data
WORKDIR /app
COPY . /app
# Tooling and dependencies live in the image; the receiver never installs from a registry.
RUN uv sync --locked --no-dev --extra agent --extra weixin --extra acp --no-editable && uv cache clean
ENV PATH="/app/.venv/bin:/opt/tools/node_modules/.bin:${PATH}" PYTHONPATH=/app/src CHATCOPILOT_SOURCE_ROOT=/app
USER application
ENTRYPOINT ["python","/app/receiver.py"]
CMD ["run"]
