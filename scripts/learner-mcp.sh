#!/bin/sh
# Start the learner MCP server from the repo root, using the project virtualenv.
cd "$(dirname "$0")/.." && exec .venv/bin/python -m learner.server
