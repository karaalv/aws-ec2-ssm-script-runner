"""Read and validate composite action inputs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


def integer(
	env: Mapping[str, str],
	name: str,
	default: int,
	minimum: int = 1,
	maximum: int | None = None,
) -> int:
	raw = env.get(name, str(default))
	if not re.fullmatch(r'[0-9]+', raw):
		raise ValueError(f'{name} must be an integer')
	value = int(raw)
	if value < minimum or (maximum is not None and value > maximum):
		raise ValueError(f'{name} is outside the supported range')
	return value


@dataclass
class Config:
	instance_id: str
	instance_name: str
	bucket: str
	key: str
	filename: str
	exports: list[tuple[str, str]]
	poll: int
	timeout: int
	cloudwatch: bool
	group: str
	drain: int

	@classmethod
	def from_env(cls, env: Mapping[str, str]) -> Config:
		bucket = env.get('INPUT_BUCKET_NAME', '')
		key = env.get('INPUT_SCRIPT_LOCATION', '')
		instance_id = env.get('INPUT_INSTANCE_ID', '')
		instance_name = env.get('INPUT_INSTANCE_NAME', '')
		if not bucket or not key:
			raise ValueError(
				'bucket-name and script-location are required'
			)
		if not instance_id and not instance_name:
			raise ValueError(
				'instance-id or instance-name is required'
			)
		if not env.get('AWS_REGION') and not env.get(
			'AWS_DEFAULT_REGION'
		):
			raise ValueError(
				'Set aws-region using configure-aws-credentials'
			)
		filename = (
			env.get('INPUT_SCRIPT_NAME') or key.rsplit('/', 1)[-1]
		)
		if (
			filename in {'', '.', '..'}
			or '/' in filename
			or any(
				ord(c) < 32 or ord(c) == 127
				for c in filename + key + bucket
			)
		):
			raise ValueError(
				'Use a plain script filename and paths '
				'without control characters'
			)
		exports: list[tuple[str, str]] = []
		for index, line in enumerate(
			env.get('INPUT_ENV_VARS', '').splitlines(), 1
		):
			if not line.strip() or line.lstrip().startswith('#'):
				continue
			name, separator, value = line.partition('=')
			if (
				not separator
				or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
				or name.startswith('SSM_RUNNER_')
			):
				raise ValueError(
					f'Invalid environment variable on line {index}'
				)
			if '\0' in value:
				raise ValueError(
					'Null character in environment variable '
					f'on line {index}'
				)
			exports.append((name, value))
		enabled = env.get('INPUT_CLOUDWATCH_LOGS', 'false').lower()
		if enabled not in {'true', 'false'}:
			raise ValueError('cloudwatch-logs must be true or false')
		group = (
			env.get('INPUT_CLOUDWATCH_LOG_GROUP')
			or '/aws/ssm/AWS-RunShellScript'
		)
		if not re.fullmatch(r'[.\-_/#A-Za-z0-9]{1,512}', group):
			raise ValueError('Invalid CloudWatch log group name')
		return cls(
			instance_id,
			instance_name,
			bucket,
			key,
			filename,
			exports,
			integer(env, 'INPUT_POLL_INTERVAL', 5),
			integer(env, 'INPUT_TIMEOUT', 1800, maximum=172800),
			enabled == 'true',
			group,
			integer(
				env,
				'INPUT_CLOUDWATCH_DRAIN',
				35,
				minimum=0,
				maximum=300,
			),
		)
