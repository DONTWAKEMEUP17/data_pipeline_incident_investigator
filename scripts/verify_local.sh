#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x .venv/bin/python ]]; then
  python_bin=.venv/bin/python
else
  python_bin=python3
fi

verification_dir="$(mktemp -d "${TMPDIR:-/tmp}/incident-investigator-verification.XXXXXX")"
trap 'rm -rf "$verification_dir"' EXIT

"$python_bin" -m unittest discover -s tests -v
env PYTHONPYCACHEPREFIX="$verification_dir/pycache" "$python_bin" -m compileall -q incident_pipeline scripts tests
"$python_bin" -m incident_pipeline.demo --workspace "$verification_dir/demo" --no-serve
git diff --check

echo "Local verification passed."
