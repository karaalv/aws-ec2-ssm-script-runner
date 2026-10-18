"""SSM submission, invocation status, cancellation and EC2 lookup."""

from __future__ import annotations

from typing import cast

from ..config import Config
from ..remote import remote_commands
from .types import (
	AwsTransport,
	InstancesPage,
	Invocation,
	Submission,
)

TERMINAL = {'Success', 'Failed', 'Cancelled', 'TimedOut'}
ACTIVE = {'Pending', 'InProgress', 'Delayed', 'Cancelling'}


class SsmClient:
	def __init__(self, aws: AwsTransport) -> None:
		self.aws = aws

	def resolve_instance(
		self,
		instance_id: str,
		instance_name: str,
	) -> str:
		if instance_id:
			return instance_id
		payload: dict[str, object] = {
			'Filters': [
				{'Name': 'tag:Name', 'Values': [instance_name]},
				{
					'Name': 'instance-state-name',
					'Values': ['running'],
				},
			]
		}
		instances: list[str] = []
		while True:
			response = cast(
				InstancesPage,
				self.aws.call(
					'ec2',
					'describe-instances',
					payload=payload,
				),
			)
			instances.extend(
				item['InstanceId']
				for reservation in response.get('Reservations', [])
				for item in reservation.get('Instances', [])
			)
			if not response.get('NextToken'):
				break
			payload['NextToken'] = response['NextToken']
		if len(instances) != 1:
			raise ValueError(
				f'Name tag matched {len(instances)} '
				'running instances; '
				'provide a unique name or instance-id'
			)
		return instances[0]

	def submit(self, config: Config) -> str:
		payload: dict[str, object] = {
			'InstanceIds': [config.instance_id],
			'DocumentName': 'AWS-RunShellScript',
			'TimeoutSeconds': max(30, config.timeout),
			'Parameters': {
				'commands': remote_commands(config),
				'executionTimeout': [str(config.timeout)],
			},
		}
		if config.cloudwatch:
			payload['CloudWatchOutputConfig'] = {
				'CloudWatchOutputEnabled': True,
				'CloudWatchLogGroupName': config.group,
			}
		# An ambiguous failure must not submit the script twice.
		response = cast(
			Submission,
			self.aws.call('ssm', 'send-command', payload=payload),
		)
		return response['Command']['CommandId']

	def invocation(
		self,
		command_id: str,
		instance_id: str,
		deadline: float,
	) -> Invocation:
		return cast(
			Invocation,
			self.aws.call(
				'ssm',
				'get-command-invocation',
				command_id=command_id,
				instance_id=instance_id,
				deadline=deadline,
			),
		)

	def cancel(
		self,
		command_id: str,
		instance_id: str,
		deadline: float,
	) -> None:
		self.aws.call(
			'ssm',
			'cancel-command',
			command_id=command_id,
			instance_ids=instance_id,
			deadline=deadline,
		)
