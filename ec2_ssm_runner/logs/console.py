"""Console output for local messages and remote log lines."""

from __future__ import annotations


def log(message: str) -> None:
	print(message, flush=True)


def remote_log(source: str, message: str) -> None:
	# Prefix lines to prevent remote GitHub workflow commands.
	for line in message.splitlines():
		log(f'[{source}] {line}')
