#!/usr/bin/env bash
# SpareAI one-click setup + run for macOS / Linux
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e .
python -m spareai validate || true
python -m spareai run
echo "Dashboard: http://localhost:${SPAREAI_PORT:-8010}"
python -m spareai serve --port "${SPAREAI_PORT:-8010}"
