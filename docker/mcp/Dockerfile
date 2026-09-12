FROM python:3.12-slim

# freecad-cmd provides a headless-only FreeCAD CLI (freecadcmd) used locally
# by execute_code_headless. This is independent of the full GUI FreeCAD that
# runs in the separate "freecad" service and hosts the RPC server.
RUN apt-get update && apt-get install -y --no-install-recommends \
        freecad-cmd \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv pip install --system --no-cache .

EXPOSE 8000

ENTRYPOINT ["freecad-mcp"]
CMD ["--transport", "streamable-http", "--port", "8000"]
