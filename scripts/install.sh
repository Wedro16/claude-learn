#!/bin/sh
# One-time setup for the claude-learn pilot.
set -e
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
git config core.hooksPath githooks
# Attribution notes travel separately from commits:
#   git push origin refs/notes/claude-attribution
#   git fetch origin refs/notes/claude-attribution:refs/notes/claude-attribution
echo "claude-learn installed. Learner data: ${CLAUDE_LEARN_HOME:-$HOME/.claude-learn}"
