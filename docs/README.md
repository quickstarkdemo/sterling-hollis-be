# Documentation Index

This directory contains project-specific documentation for the Sterling Hollis
backend.

## Active Docs

- `frontend-api.md`: retail frontend integration guide and implementation notes.
- `frontend-openapi.yaml`: curated frontend OpenAPI contract for shopper-facing
  and explicitly marked operator/admin endpoints.
- `openapi.json`: generated FastAPI OpenAPI export. Refresh with `make openapi`.
- `chat-flow.excalidraw`: storefront chat flow diagram.
- `datadog-reference-tables/`: Datadog reference table import files and notes.

## Source Of Truth

- Runtime configuration: `.env.example` and `app/config.py`.
- Generated API shape: `docs/openapi.json`.
- Curated frontend contract: `docs/frontend-openapi.yaml`.
- MCP tool surface: `app/mcp_server.py`, the MCP and Apps SDK section in the
  repository `README.md`, or `make mcp-smoke` against a running app.
- Daily synthetic order refresh: the "Keep Demo Orders Current" section in the
  repository `README.md`.

External OpenAI, ChatGPT Apps SDK, and MCP reference pages are not vendored here.
Use the official upstream docs for platform behavior so local docs stay focused
on this project.
