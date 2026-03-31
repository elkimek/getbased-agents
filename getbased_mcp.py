#!/usr/bin/env python3
"""getbased MCP server — exposes blood work data as tools."""

import os
import re

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("getbased")

TOKEN = os.environ.get("GETBASED_TOKEN", "")
GATEWAY = os.environ.get("GETBASED_GATEWAY", "https://sync.getbased.health")


async def _fetch_context() -> dict:
    if not TOKEN:
        return {"error": "GETBASED_TOKEN not set"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{GATEWAY}/api/context",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        r.raise_for_status()
        return r.json()


def _parse_sections(context: str) -> dict[str, str]:
    """Returns {full_name: content}"""
    sections = {}
    for m in re.finditer(
        r"\[section:(\S+)([^\]]*)\]([\s\S]*?)\[/section:\1\]", context
    ):
        base = m.group(1)
        meta = m.group(2).strip()
        full_name = f"{base} {meta}" if meta else base
        sections[full_name] = m.group(3).strip()
    return sections


@mcp.tool()
async def getbased_lab_context() -> str:
    """Get a full summary of the user's blood work data, health context,
    supplements, and goals from getbased. Use when the user asks broad
    questions about their labs, biomarkers, or health trends."""
    data = await _fetch_context()
    if "error" in data:
        return f"Error: {data['error']}"
    parts = []
    if data.get("updatedAt"):
        parts.append(f"Updated: {data['updatedAt']}")
    parts.append(data.get("context", "No context available"))
    return "\n\n".join(parts)


@mcp.tool()
async def getbased_section(section: str = "") -> str:
    """Get a specific section of health data, or list all available sections.
    Call with no section name to get the index (section names + line counts).
    Call with a section name to get just that section's content.
    Sections include: biometrics, hormones, lipids, hematology, biochemistry,
    supplements, goals, genetics, context cards, etc.
    Section names are matched by prefix."""
    data = await _fetch_context()
    if "error" in data:
        return f"Error: {data['error']}"
    context = data.get("context", "")
    if not context:
        return "No context available"

    sections = _parse_sections(context)

    if not section:
        lines = []
        for name, content in sections.items():
            count = len([l for l in content.split("\n") if l.strip()])
            lines.append(f"  {name}  ({count} lines)")
        return "Available sections:\n\n" + "\n".join(lines)

    query = section.lower().strip()
    match_key = None
    for k in sections:
        if k.lower() == query:
            match_key = k
            break
    if not match_key:
        for k in sections:
            if k.lower().startswith(query):
                match_key = k
                break
    if not match_key:
        available = [k.split(" ")[0] for k in sections]
        return f'Section "{section}" not found\nAvailable: {", ".join(available)}'

    return f"[{match_key}]\n\n{sections[match_key]}"


@mcp.tool()
async def getbased_list_profiles() -> str:
    """List all available profiles in getbased."""
    data = await _fetch_context()
    if "error" in data:
        return f"Error: {data['error']}"
    profiles = data.get("profiles") or []
    if not profiles:
        return "No profiles found"
    return "\n".join(
        f"{p.get('id', '?')}  {p.get('name', 'unnamed')}" for p in profiles
    )


if __name__ == "__main__":
    mcp.run()
