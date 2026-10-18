from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from ec2_ssm_runner.aws import cli as aws_cli
from ec2_ssm_runner.errors import AwsError


class AwsCliTests(unittest.TestCase):
	def test_private_payload_and_no_submission_retry(self) -> None:
		completed = subprocess.CompletedProcess(
			[], 0, '{"Command": {"CommandId": "cmd"}}', ''
		)
		recorded = {}

		def invoke(
			args: list[str],
			**kwargs: object,
		) -> subprocess.CompletedProcess[str]:
			path = Path(
				args[args.index('--cli-input-json') + 1].removeprefix(
					'file://'
				)
			)
			recorded['path'] = path
			recorded['payload'] = json.loads(path.read_text())
			recorded['mode'] = path.stat().st_mode & 0o777
			return completed

		with patch.object(
			aws_cli.subprocess, 'run', side_effect=invoke
		) as run:
			aws_cli.AwsCli().call(
				'ssm',
				'send-command',
				payload={'secret': 'sensitive-value'},
			)
		args, kwargs = run.call_args
		self.assertNotIn('sensitive-value', ' '.join(args[0]))
		self.assertEqual(
			recorded['payload'], {'secret': 'sensitive-value'}
		)
		self.assertEqual(recorded['mode'], 0o600)
		self.assertFalse(recorded['path'].exists())
		self.assertEqual(kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
		self.assertEqual(kwargs['timeout'], 30)

	def test_cli_error_codes_are_classified(self) -> None:
		completed = subprocess.CompletedProcess(
			[],
			254,
			'',
			'An error occurred (AccessDeniedException) '
			'when calling API: denied',
		)
		with (
			patch.object(
				aws_cli.subprocess, 'run', return_value=completed
			),
			self.assertRaises(AwsError) as error,
		):
			aws_cli.AwsCli().call('ssm', 'get-command-invocation')
		self.assertEqual(
			error.exception.code, 'AccessDeniedException'
		)

	def test_client_calls_are_bounded_by_monitoring_deadline(
		self,
	) -> None:
		with (
			patch.object(aws_cli.time, 'monotonic', return_value=10),
			patch.object(
				aws_cli.subprocess,
				'run',
				side_effect=subprocess.TimeoutExpired('aws', 2),
			) as run,
			self.assertRaises(AwsError) as error,
		):
			aws_cli.AwsCli().call(
				'ssm', 'get-command-invocation', deadline=12
			)
		self.assertEqual(run.call_args.kwargs['timeout'], 2)
		self.assertEqual(error.exception.code, 'ClientTimeout')
