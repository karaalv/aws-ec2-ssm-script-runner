"""GitHub Actions output values and secret masking."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .logs.console import log


def workflow_escape(value: str) -> str:
	return (
		value.replace('%', '%25')
		.replace('\r', '%0D')
		.replace('\n', '%0A')
	)


def output(**values: str | int) -> None:
	path = os.environ.get('GITHUB_OUTPUT')
	if path:
		with open(path, 'a', encoding='utf-8') as file:
			for key, value in values.items():
				file.write(f'{key}={value}\n')


def mask_values(exports: Iterable[tuple[str, str]]) -> None:
	if os.environ.get('GITHUB_ACTIONS') == 'true':
		for _, value in exports:
			if value:
				log('::add-mask::' + workflow_escape(value))
