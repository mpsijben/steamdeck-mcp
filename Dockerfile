# For MCP directories (e.g. Glama) that start the server to check it boots and to list its tools.
# steamdeck-mcp is meant to run directly on a Steam Deck; inside a container the tools have no
# Deck to talk to, so this image is only useful for introspection, not for real use.
FROM python:3.12-slim

WORKDIR /app
COPY server.py ./
COPY steamdeck_mcp ./steamdeck_mcp

# "full" exposes every tool in tools/list so the directory listing shows the complete set.
ENV STEAMDECK_MCP_LEVEL=full

CMD ["python3", "-I", "server.py"]
