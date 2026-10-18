"""Shared AWS responses, action inputs and clock fixtures."""

import sys
from pathlib import Path

from ec2_ssm_runner.config import Config

BASE_ENV = {
	'AWS_REGION': 'eu-west-1',
	'INPUT_INSTANCE_ID': 'i-example',
	'INPUT_BUCKET_NAME': 'example-scripts',
	'INPUT_SCRIPT_LOCATION': 'deploy/run.sh',
	'INPUT_POLL_INTERVAL': '1',
	'INPUT_TIMEOUT': '10',
}


def config(**values):
	return Config.from_env(dict(BASE_ENV, **values))


def snapshot(status='Success', code=0, stdout='', stderr=''):
	return dict(
		Status=status,
		ResponseCode=code,
		StandardOutputContent=stdout,
		StandardErrorContent=stderr,
	)


class Clock:
	def __init__(self):
		self.now = 0

	def monotonic(self):
		return self.now

	def sleep(self, seconds):
		self.now += seconds


class FakeAws:
	def __init__(self, responses):
		self.responses = {
			key: list(values) for key, values in responses.items()
		}
		self.calls = []

	def call(self, service, operation, **kwargs):
		self.calls.append((service, operation, kwargs))
		values = self.responses.get(operation)
		if not values:
			raise AssertionError(
				f'Unexpected AWS call: {operation} {kwargs}'
			)
		value = values.pop(0)
		if isinstance(value, BaseException):
			raise value
		return value


def stream_page(name):
	return {'logStreams': [{'logStreamName': name}]}


def events(token, *messages):
	return {
		'nextForwardToken': token,
		'events': [{'message': m} for m in messages],
	}


def make_aws(root):
	binary = root / 'aws'
	fixture = Path(__file__).parent / 'fixtures' / 'aws.py'
	binary.write_text(f'#!{sys.executable}\n' + fixture.read_text())
	binary.chmod(0o755)
	return binary
