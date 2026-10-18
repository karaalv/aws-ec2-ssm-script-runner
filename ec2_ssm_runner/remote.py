"""Build the shell commands executed by SSM on the instance."""

from __future__ import annotations

import shlex

from .config import Config


def remote_commands(config: Config) -> list[str]:
	filename = shlex.quote(config.filename)
	uri = shlex.quote(f's3://{config.bucket}/{config.key}')
	exports = [
		f'export {name}={shlex.quote(value)}'
		for name, value in config.exports
	]
	return [
		'set -eu',
		'SSM_RUNNER_DIR=$(mktemp -d '
		'"${TMPDIR:-/tmp}/ssm-script.XXXXXXXX")',
		"trap 'result=$?; trap - EXIT; "
		'rm -rf -- "$SSM_RUNNER_DIR" || '
		'echo "[runner] Cleanup failed" >&2; '
		'exit "$result"\' EXIT',
		"trap 'exit 130' INT",
		"trap 'exit 143' TERM",
		'echo "[runner] Downloading script from S3"',
		f'SSM_RUNNER_SCRIPT="$SSM_RUNNER_DIR"/{filename}',
		f'aws s3 cp -- {uri} "$SSM_RUNNER_SCRIPT"',
		'chmod +x "$SSM_RUNNER_SCRIPT"',
		'readonly SSM_RUNNER_DIR SSM_RUNNER_SCRIPT',
		*exports,
		'echo "[runner] Running script"',
		'"$SSM_RUNNER_SCRIPT"',
	]
