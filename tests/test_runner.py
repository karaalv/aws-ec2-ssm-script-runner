import contextlib
import io
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ec2_ssm_runner import runner
from ec2_ssm_runner.errors import AwsError, Interrupted
from tests.support import (
	Clock,
	FakeAws,
	config,
	events,
	snapshot,
	stream_page,
)


class RunnerTests(unittest.TestCase):
	def run_action(self, aws, cfg=None):
		clock = Clock()
		with tempfile.TemporaryDirectory() as directory:
			output = Path(directory) / 'outputs'
			with (
				patch.dict(
					os.environ, {'GITHUB_OUTPUT': str(output)}
				),
				patch.object(
					runner.time, 'monotonic', clock.monotonic
				),
				patch.object(runner.time, 'sleep', clock.sleep),
				contextlib.redirect_stdout(io.StringIO()) as logs,
			):
				result = runner.Runner(cfg or config(), aws).run()
			return (
				result,
				logs.getvalue(),
				output.read_text() if output.exists() else '',
			)

	def test_poll_once_per_cycle_and_preserve_both_streams(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot('InProgress', -1),
					snapshot(stdout='hello\n', stderr='warning\n'),
				],
			}
		)
		result, logs, output = self.run_action(aws)
		self.assertEqual(result, 0)
		self.assertIn('[ssm/stdout] hello', logs)
		self.assertIn('[ssm/stderr] warning', logs)
		self.assertEqual(
			sum(c[1] == 'get-command-invocation' for c in aws.calls),
			2,
		)
		self.assertTrue(output.startswith('command-id=cmd\n'))
		self.assertIn('status=Success\nexit-code=0', output)
		payload = aws.calls[0][2]['payload']
		self.assertNotIn('CloudWatchOutputConfig', payload)
		self.assertEqual(
			payload['Parameters']['executionTimeout'], ['10']
		)

	def test_ssm_prints_only_new_text(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot('InProgress', -1, 'first\n'),
					snapshot(stdout='first\nsecond\n'),
				],
			}
		)
		_, logs, _ = self.run_action(aws)
		self.assertEqual(logs.count('[ssm/stdout] first'), 1)
		self.assertEqual(logs.count('[ssm/stdout] second'), 1)

	def test_propagation_and_throttling_are_retried(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					AwsError('InvocationDoesNotExist', 'not yet'),
					AwsError('ThrottlingException', 'slow down'),
					snapshot(),
				],
			}
		)
		result, logs, _ = self.run_action(aws)
		self.assertEqual(result, 0)
		self.assertIn('ThrottlingException', logs)

	def test_permission_failure_is_visible_and_cancels(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					AwsError('AccessDeniedException', 'denied'),
					snapshot('InProgress', -1, 'last output'),
				],
				'cancel-command': [{}],
			}
		)
		result, logs, output = self.run_action(aws)
		self.assertEqual(result, 1)
		self.assertIn('AccessDeniedException', logs)
		self.assertIn('[ssm/stdout] last output', logs)
		self.assertIn('status=MonitoringFailed', output)
		self.assertEqual(
			sum(c[1] == 'cancel-command' for c in aws.calls), 1
		)

	def test_timeout_attempts_cancellation(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [snapshot('InProgress', -1)]
				* 3,
				'cancel-command': [{}],
			}
		)
		result, _, output = self.run_action(
			aws, config(INPUT_TIMEOUT='2')
		)
		self.assertEqual(result, 1)
		self.assertIn('status=TimedOut', output)
		self.assertEqual(
			sum(c[1] == 'cancel-command' for c in aws.calls), 1
		)

	def test_interruption_cancels_and_preserves_output(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					Interrupted(signal.SIGTERM),
					snapshot('Cancelled', -1, 'before interruption'),
				],
				'cancel-command': [{}],
			}
		)
		result, logs, output = self.run_action(aws)
		self.assertEqual(result, 143)
		self.assertIn('[ssm/stdout] before interruption', logs)
		self.assertIn('status=Cancelled', output)

	def test_retries_are_bounded_and_cancel_errors_are_visible(self):
		error = AwsError('ThrottlingException', 'slow down')
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [error] * 5
				+ [snapshot('InProgress', -1)],
				'cancel-command': [
					AwsError('AccessDeniedException', 'cannot cancel')
				],
			}
		)
		result, logs, output = self.run_action(aws)
		self.assertEqual(result, 1)
		self.assertIn('attempt 5/5', logs)
		self.assertIn('Cancellation could not be confirmed', logs)
		self.assertIn('status=MonitoringFailed', output)

	def test_remote_failure_and_nonzero_success_fail_action(self):
		for status in ('Failed', 'Success'):
			with self.subTest(status=status):
				aws = FakeAws(
					{
						'send-command': [
							{'Command': {'CommandId': 'cmd'}}
						],
						'get-command-invocation': [
							snapshot(
								status, 7, stderr='script failed'
							)
						],
					}
				)
				result, logs, output = self.run_action(aws)
				self.assertEqual(result, 1)
				self.assertIn('[ssm/stderr] script failed', logs)
				self.assertIn('exit-code=7', output)

	def test_cancelling_is_not_a_terminal_state(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot('Cancelling', -1),
					snapshot('Cancelled', -1),
				],
			}
		)
		result, logs, _ = self.run_action(aws)
		self.assertEqual(result, 1)
		self.assertIn('SSM status: Cancelled', logs)

	def test_submission_is_not_retried(self):
		aws = FakeAws(
			{'send-command': [AwsError('ClientTimeout', 'timeout')]}
		)
		result, logs, output = self.run_action(aws)
		self.assertEqual(result, 1)
		self.assertEqual(len(aws.calls), 1)
		self.assertEqual(output, '')
		self.assertIn('check SSM before rerunning', logs)

	def test_cloudwatch_denial_uses_ssm_backup(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot('InProgress', -1),
					snapshot(stdout='backup'),
				],
				'describe-log-streams': [
					AwsError('AccessDeniedException', 'denied')
				],
			}
		)
		result, logs, _ = self.run_action(
			aws, config(INPUT_CLOUDWATCH_LOGS='true')
		)
		self.assertEqual(result, 0)
		self.assertIn('streaming disabled', logs)
		self.assertIn('[ssm/stdout] backup', logs)
		self.assertTrue(
			aws.calls[0][2]['payload']['CloudWatchOutputConfig'][
				'CloudWatchOutputEnabled'
			]
		)

	def test_drain_keeps_late_events_and_ssm_backup(self):
		name = 'cmd/i-example/plugin/stdout'
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot(stdout='complete')
				],
				'describe-log-streams': [stream_page(name)] * 3,
				'get-log-events': [
					events('1'),
					events('1'),
					events('2', 'late'),
					events('2'),
					events('2'),
				],
			}
		)
		result, logs, _ = self.run_action(
			aws,
			config(
				INPUT_CLOUDWATCH_LOGS='true',
				INPUT_CLOUDWATCH_DRAIN='3',
			),
		)
		self.assertEqual(result, 0)
		self.assertEqual(logs.count('[cloudwatch/stdout] late'), 1)
		self.assertIn('[ssm/stdout] complete', logs)

	def test_ssm_truncation_is_reported(self):
		aws = FakeAws(
			{
				'send-command': [{'Command': {'CommandId': 'cmd'}}],
				'get-command-invocation': [
					snapshot(stdout='x' * 24000)
				],
			}
		)
		_, logs, _ = self.run_action(aws)
		self.assertIn('24000-character limit', logs)

	def test_instance_name_resolution_checks_every_page(self):
		aws = FakeAws(
			{
				'describe-instances': [
					{
						'Reservations': [
							{'Instances': [{'InstanceId': 'i-one'}]}
						],
						'NextToken': 'next',
					},
					{
						'Reservations': [
							{'Instances': [{'InstanceId': 'i-two'}]}
						]
					},
				]
			}
		)
		result, logs, _ = self.run_action(
			aws,
			config(
				INPUT_INSTANCE_ID='', INPUT_INSTANCE_NAME='shared'
			),
		)
		self.assertEqual(result, 1)
		self.assertIn('matched 2', logs)
		self.assertEqual(len(aws.calls), 2)
