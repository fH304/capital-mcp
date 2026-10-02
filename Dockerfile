FROM python:3.10-alpine@sha256:07a3e27a565ce2397efe9a371c9cd8d675827cb1151fe12ffc7498b447712c56 AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY capital_mcp/ capital_mcp/

RUN pip install --no-cache-dir --disable-pip-version-check \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    uv==0.11.16 \
 && uv export --frozen --offline --no-dev --no-emit-project -o requirements.txt \
 && pip install --no-cache-dir --disable-pip-version-check --no-deps \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt \
 && pip install --no-cache-dir --disable-pip-version-check --no-deps \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    . \
 && pip uninstall -y uv


FROM python:3.10-alpine@sha256:07a3e27a565ce2397efe9a371c9cd8d675827cb1151fe12ffc7498b447712c56

ARG VERSION=0.1.0
ARG SOURCE_URL=https://github.com/capital-com-sv/capital-mcp

LABEL org.opencontainers.image.title="Capital.com MCP Server" \
      org.opencontainers.image.description="MCP server for Capital.com Open API — LLM-driven trading via Model Context Protocol" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.url="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="Capital.com"

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /app/capital_mcp /app/capital_mcp

RUN adduser -D -s /bin/sh mcp
USER mcp

RUN python -c "import importlib, pkgutil, capital_mcp; [importlib.import_module(m.name) for m in pkgutil.iter_modules(capital_mcp.__path__, 'capital_mcp.') if not m.name.endswith('.__main__')]"

ENTRYPOINT ["python", "-m", "capital_mcp.remote"]
