"""SSM submission, invocation status, cancellation and EC2 lookup."""

from ..remote import remote_commands

TERMINAL = {'Success', 'Failed', 'Cancelled', 'TimedOut'}
ACTIVE = {'Pending', 'InProgress', 'Delayed', 'Cancelling'}


class SsmClient:
	def __init__(self, aws):
		self.aws = aws

	def resolve_instance(self, instance_id, instance_name):
		if instance_id:
			return instance_id
		payload = {
			'Filters': [
				{'Name': 'tag:Name', 'Values': [instance_name]},
				{
					'Name': 'instance-state-name',
					'Values': ['running'],
				},
			]
		}
		instances = []
		while True:
			response = self.aws.call(
				'ec2',
				'describe-instances',
				payload=payload,
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

	def submit(self, config):
		payload = {
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
		response = self.aws.call(
			'ssm',
			'send-command',
			payload=payload,
		)
		return response['Command']['CommandId']

	def invocation(self, command_id, instance_id, deadline):
		return self.aws.call(
			'ssm',
			'get-command-invocation',
			command_id=command_id,
			instance_id=instance_id,
			deadline=deadline,
		)

	def cancel(self, command_id, instance_id, deadline):
		self.aws.call(
			'ssm',
			'cancel-command',
			command_id=command_id,
			instance_ids=instance_id,
			deadline=deadline,
		)
