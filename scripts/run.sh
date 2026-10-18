#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found on runner" >&2
    exit 2
fi

exec python3 -u "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/runner.py"
