"""Allowlisted MCP dispatch for planning records, adapted from MCP Gateway."""

import argparse
import asyncio
from dataclasses import asdict
import json
import sqlite3

from .journal import Journal
from .messages import VERSION, parse_mcp
from .methods import QUERY, local_notes
from .research import validate_id
from .stat_tools import TOOL_SEQUENCE


def dispatch(name: str, args: dict, journal: Journal | None = None, run_id: str | None = None) -> dict:
    request = parse_mcp(name, args)
    if request.name == "method_notes_search":
        return {"version": VERSION, "notes": [asdict(note) for note in local_notes(request.topic)]}
    if request.name == "branch_records" and journal is not None and run_id is not None:
        plan = journal.get(validate_id(run_id), "plan")
        if plan is not None and plan.get("run_id") != run_id:
            raise ValueError("Stored plan belongs to another run")
        branches = [{key: branch[key] for key in ("id", "parent_id", "decisions", "score",
                                                 "reason", "depth", "retained")} for branch in plan["branches"]] if plan else []
        return {"version": VERSION, "branches": branches, "status": "found" if plan else "not_found"}
    if request.name == "statistical_tool_catalog":
        return {"version": VERSION, "tools": list(TOOL_SEQUENCE), "execution": "requires a controller-owned, approved run",
                "holdout": "locked"}
    raise ValueError("Tool unavailable or arguments outside its allowed schema")


def build_server(journal: Journal | None = None, run_id: str | None = None):
    from mcp import types
    from mcp.server import Server

    if (journal is None) != (run_id is None):
        raise ValueError("A branch view requires one host-selected run")
    if run_id is not None:
        validate_id(run_id)
    server = Server("agentic-edgelab-planning")
    version = {"type": "integer", "const": VERSION}
    empty = {"type": "object", "properties": {"version": version}, "required": ["version"],
             "additionalProperties": False}
    definitions = [
        types.Tool(name="method_notes_search", description="Read bundled public-method summaries.", inputSchema={
            "type": "object", "properties": {"version": version, "topic": {"type": "string", "enum": list(QUERY)}},
            "required": ["version", "topic"], "additionalProperties": False,
        }),
        types.Tool(name="statistical_tool_catalog", description="Inspect the statistical tool allowlist.", inputSchema=empty),
    ]
    if run_id is not None:
        definitions.append(types.Tool(name="branch_records", description="Read only the host-selected planning trace.",
                                      inputSchema=empty))

    @server.list_tools()
    async def list_tools():
        return definitions

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        try:
            result = dispatch(name, arguments, journal, run_id)
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return types.CallToolResult(isError=True, content=[
                types.TextContent(type="text", text="Tool unavailable or invalid arguments"),
            ])
        return types.CallToolResult(structuredContent=result, content=[
            types.TextContent(type="text", text=json.dumps(result)),
        ])

    return server


def main() -> None:
    from pathlib import Path
    from mcp.server.stdio import stdio_server

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path(__file__).resolve().parent.parent / "runtime/edgelab.sqlite",
                        help="Read the host-selected local journal")
    parser.add_argument("--run-id", type=validate_id, help="Scope branch visibility to one local run")
    args = parser.parse_args()
    path = args.journal
    if args.run_id and not path.is_file():
        parser.error("No EdgeLab run journal exists")
    server = build_server(Journal(path, read_only=True) if args.run_id else None, args.run_id)

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())


if __name__ == "__main__":
    main()
