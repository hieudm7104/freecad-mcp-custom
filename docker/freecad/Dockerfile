FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        freecad \
        xvfb \
        mesa-utils \
        libgl1-mesa-dri \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY addon/FreeCADMCP /opt/freecad-mcp-addon/FreeCADMCP
COPY docker/freecad/settings.seed.json /opt/freecad-mcp-addon/settings.seed.json
COPY docker/freecad/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV HOME=/root

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
