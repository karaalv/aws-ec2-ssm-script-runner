#!/usr/bin/env bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found; the action must set up Python first" >&2
    exit 2
fi

# Run our package from the action checkout, independent of caller files.
action_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$action_root"
exec python3 -u -m ec2_ssm_runner
