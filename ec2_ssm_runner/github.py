"""GitHub Actions output values and secret masking."""

import os

from .logs.console import log


def workflow_escape(value):
	return (
		value.replace('%', '%25')
		.replace('\r', '%0D')
		.replace('\n', '%0A')
	)


def output(**values):
	path = os.environ.get('GITHUB_OUTPUT')
	if path:
		with open(path, 'a', encoding='utf-8') as file:
			for key, value in values.items():
				file.write(f'{key}={value}\n')


def mask_values(exports):
	if os.environ.get('GITHUB_ACTIONS') == 'true':
		for _, value in exports:
			if value:
				log('::add-mask::' + workflow_escape(value))
