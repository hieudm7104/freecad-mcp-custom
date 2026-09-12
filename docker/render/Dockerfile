FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        blender \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/render/render.py /opt/render/render.py

ENTRYPOINT ["blender", "-b", "-P", "/opt/render/render.py", "--"]
