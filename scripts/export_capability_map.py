#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict
import argparse
import os
from pathlib import Path
import sys


FRONTEND_SURFACES = {"rest", "chat", "admin_assistant", "mcp", "widget"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the Sterling capability map to Markdown.")
    parser.add_argument(
        "--output",
        default="docs/capability-map.md",
        help="Output path for the generated Markdown capability map.",
    )
    return parser.parse_args()


def _cell(value: object) -> str:
    text = str(value or "-").replace("\n", " ").replace("|", "\\|")
    return text if text else "-"


def _join(values: list[str] | tuple[str, ...] | set[str]) -> str:
    ordered = sorted(str(value) for value in values if str(value))
    return "<br>".join(ordered) if ordered else "-"


def _operation_rows(schema: dict) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = defaultdict(list)
    for path, methods in schema.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            capability_id = operation.get("x-sterling-capability-id")
            if not capability_id:
                continue
            surface = operation.get("x-sterling-api-surface", "unknown")
            current = "current" if operation.get("x-sterling-current-frontend-contract") else "compat"
            rows[str(capability_id)].append(f"`{method.upper()} {path}` ({surface}, {current})")
    return rows


def _mcp_rows() -> dict[str, list[str]]:
    from app.mcp_server import MCP_BUNDLE_DEFINITIONS, MCP_TOOL_CAPABILITY_IDS

    bundles_by_tool: dict[str, list[str]] = defaultdict(list)
    for definition in MCP_BUNDLE_DEFINITIONS:
        for tool_name in definition.tool_names:
            bundles_by_tool[tool_name].append(definition.path + "/")

    rows: dict[str, list[str]] = defaultdict(list)
    for tool_name, capability_id in MCP_TOOL_CAPABILITY_IDS.items():
        bundles = ", ".join(sorted(bundles_by_tool.get(tool_name, ()))) or "local `/mcp`"
        rows[capability_id].append(f"`{tool_name}` ({bundles})")
    return rows


def _agent_rows() -> dict[str, list[str]]:
    from app.services.capability_executor import CHAT_TOOL_CAPABILITIES

    rows: dict[str, list[str]] = defaultdict(list)
    for tool_name, capability_id in CHAT_TOOL_CAPABILITIES.items():
        rows[capability_id].append(f"`{tool_name}`")
    return rows


def _schema() -> dict:
    os.environ["ENABLE_MCP_ADAPTER"] = "false"
    os.environ["ENABLE_OPENAI_APPS_UI"] = "false"

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    return create_app().openapi()


def _render(output_path: Path) -> str:
    from app.services.capabilities import REGISTRY_VERSION, list_capabilities

    schema = _schema()
    rest_by_capability = _operation_rows(schema)
    mcp_by_capability = _mcp_rows()
    agent_by_capability = _agent_rows()

    lines = [
        "# Sterling Capability Map",
        "",
        "Generated from `app.services.capabilities`, FastAPI route metadata, chat tool routing, and MCP bundle metadata.",
        "",
        f"- Registry version: `{REGISTRY_VERSION}`",
        f"- OpenAPI source: `docs/openapi.json` / runtime `/openapi.json`",
        f"- Generated file: `{output_path.as_posix()}`",
        "",
        "## Surface Classification",
        "",
        "| Surface | Frontend meaning |",
        "| --- | --- |",
        "| `rest` | HTTP API routes documented in `docs/openapi.json` and the curated `docs/frontend-openapi.yaml`. |",
        "| `chat` | Storefront agent turns and deterministic chat tools surfaced through `/api/chat`. |",
        "| `admin_assistant` | Catalog Studio assistant calls surfaced inside authenticated admin workflows. |",
        "| `mcp` | Persona-scoped MCP tools; public remote clients should use scoped `/mcp/*/` endpoints. |",
        "| `widget` | OpenAI Apps SDK widget helpers and resources. |",
        "",
        "## Capability Index",
        "",
        "| Capability | Personas | Surfaces | Side effect | Approval | Input | Output | REST paths | Agent tools | MCP tools | Handler |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    frontend_capabilities = []
    for capability in list_capabilities():
        surfaces = [surface.value for surface in capability.surfaces]
        if not set(surfaces).intersection(FRONTEND_SURFACES):
            continue
        frontend_capabilities.append(capability)

    for capability in sorted(frontend_capabilities, key=lambda item: item.id):
        surfaces = [surface.value for surface in capability.surfaces]
        approval = (
            f"{capability.approval_mode.value} via `{capability.approval_field}`"
            if capability.requires_approval
            else "none"
        )
        if capability.required_grants:
            approval += f"; grants: {_join([grant.value for grant in capability.required_grants])}"
        row = [
            f"`{capability.id}`<br>{_cell(capability.name)}",
            _join([persona.value for persona in capability.allowed_personas]),
            _join(surfaces),
            capability.side_effect.value,
            approval,
            f"`{_cell(capability.input_schema)}`",
            f"`{_cell(capability.output_schema)}`",
            _join(rest_by_capability.get(capability.id, [])),
            _join(agent_by_capability.get(capability.id, [])),
            _join(mcp_by_capability.get(capability.id, [])),
            f"`{_cell(capability.service_handler)}`",
        ]
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Public shopper and public MCP capabilities must not require caller-supplied customer identity.",
            "- Authenticated shopper account capabilities derive customer identity from Clerk-backed backend state.",
            "- Send-capable MCP bundles are separate from read/write preparation bundles and require explicit approval policy.",
            "- Operator compatibility routes are documented for local/trusted workflows and are not primary frontend contracts.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_render(output_path), encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
