"""Bounded AWS CLI calls and structured error handling."""

import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import ExitStack

from ..errors import AwsError, DeadlineExceeded


class AwsCli:
	def call(
		self,
		service,
		operation,
		*,
		payload=None,
		deadline=None,
		**params,
	):
		remaining = (
			30 if deadline is None else deadline - time.monotonic()
		)
		if remaining <= 0:
			raise DeadlineExceeded('Monitoring deadline reached')
		args = [
			'aws',
			service,
			operation,
			'--output',
			'json',
			'--no-cli-pager',
			'--no-paginate',
			'--cli-connect-timeout',
			'5',
			'--cli-read-timeout',
			'20',
		]
		for key, value in params.items():
			option = '--' + key.replace('_', '-')
			args += (
				[option] if value is True else [option, str(value)]
			)
		attempts = '1' if operation == 'send-command' else '2'
		env = dict(
			os.environ,
			AWS_PAGER='',
			AWS_MAX_ATTEMPTS=attempts,
			AWS_RETRY_MODE='standard',
		)
		try:
			with ExitStack() as stack:
				if payload is not None:
					# Keep payload values out of process arguments.
					file = stack.enter_context(
						tempfile.NamedTemporaryFile(
							mode='w',
							encoding='utf-8',
							prefix='ssm-runner-',
							suffix='.json',
						)
					)
					json.dump(payload, file)
					file.flush()
					args += [
						'--cli-input-json',
						'file://' + file.name,
					]
				result = subprocess.run(
					args,
					stdin=subprocess.DEVNULL,
					capture_output=True,
					text=True,
					env=env,
					timeout=min(30, remaining),
				)
		except subprocess.TimeoutExpired as exc:
			raise AwsError(
				'ClientTimeout', 'AWS CLI request timed out'
			) from exc
		if result.returncode:
			match = re.search(
				r'An error occurred \(([^)]+)\)', result.stderr
			)
			code = match.group(1) if match else 'CliError'
			if any(
				s in result.stderr
				for s in (
					'Could not connect to the endpoint',
					'Connection was closed',
					'Connect timeout',
					'Read timeout',
				)
			):
				code = 'ConnectionError'
			raise AwsError(code, result.stderr.strip())
		try:
			return json.loads(result.stdout)
		except json.JSONDecodeError as exc:
			raise AwsError(
				'InvalidResponse', 'AWS CLI returned invalid JSON'
			) from exc
