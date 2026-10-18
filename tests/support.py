"""Shared AWS responses, action inputs and clock fixtures."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from ec2_ssm_runner.aws.types import (
	Invocation,
	LogEventsPage,
	LogStreamsPage,
)
from ec2_ssm_runner.config import Config

BASE_ENV = {
	'AWS_REGION': 'eu-west-1',
	'INPUT_INSTANCE_ID': 'i-example',
	'INPUT_BUCKET_NAME': 'example-scripts',
	'INPUT_SCRIPT_LOCATION': 'deploy/run.sh',
	'INPUT_POLL_INTERVAL': '1',
	'INPUT_TIMEOUT': '10',
}


def config(**values: str) -> Config:
	return Config.from_env(dict(BASE_ENV, **values))


def snapshot(
	status: str = 'Success',
	code: int = 0,
	stdout: str = '',
	stderr: str = '',
) -> Invocation:
	return Invocation(
		Status=status,
		ResponseCode=code,
		StandardOutputContent=stdout,
		StandardErrorContent=stderr,
	)


class Clock:
	def __init__(self) -> None:
		self.now: float = 0

	def monotonic(self) -> float:
		return self.now

	def sleep(self, seconds: float) -> None:
		self.now += seconds


class FakeAws:
	def __init__(
		self,
		responses: Mapping[str, Sequence[object]],
	) -> None:
		self.responses = {
			key: list(values) for key, values in responses.items()
		}
		self.calls: list[tuple[str, str, dict[str, Any]]] = []

	def call(
		self,
		service: str,
		operation: str,
		**kwargs: Any,
	) -> dict[str, Any]:
		self.calls.append((service, operation, kwargs))
		values = self.responses.get(operation)
		if not values:
			raise AssertionError(
				f'Unexpected AWS call: {operation} {kwargs}'
			)
		value = values.pop(0)
		if isinstance(value, BaseException):
			raise value
		return cast(dict[str, Any], value)


def stream_page(name: str) -> LogStreamsPage:
	return {'logStreams': [{'logStreamName': name}]}


def events(token: str, *messages: str) -> LogEventsPage:
	return {
		'nextForwardToken': token,
		'events': [{'message': m} for m in messages],
	}


def make_aws(root: Path) -> Path:
	binary = root / 'aws'
	fixture = Path(__file__).parent / 'fixtures' / 'aws.py'
	binary.write_text(f'#!{sys.executable}\n' + fixture.read_text())
	binary.chmod(0o755)
	return binary
