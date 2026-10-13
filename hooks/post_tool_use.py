#!/usr/bin/env python3
"""PostToolUse hook for Write/Edit/MultiEdit: remember the lines Claude wrote, for attribution."""

from _common import read_input

from learner.attribution import record_claude_lines
from learner.db import connect


def written_text(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        return tool_input.get("new_string", "")
    if tool_name == "MultiEdit":
        return "\n".join(e.get("new_string", "") for e in tool_input.get("edits", []))
    return ""


def main() -> None:
    data = read_input()
    tool_input = data.get("tool_input") or {}
    text = written_text(data.get("tool_name", ""), tool_input)
    path = tool_input.get("file_path")
    if not text or not path:
        return
    conn = connect()
    with conn:
        record_claude_lines(conn, data.get("session_id", "unknown"), path, text)


if __name__ == "__main__":
    main()
