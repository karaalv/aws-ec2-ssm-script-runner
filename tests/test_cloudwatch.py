from __future__ import annotations

import contextlib
import io
import unittest

from ec2_ssm_runner.errors import AwsError
from ec2_ssm_runner.logs.cloudwatch import CloudWatch
from ec2_ssm_runner.logs.console import remote_log
from tests.support import (
	FakeAws,
	events,
	stream_page,
)


class CloudWatchTests(unittest.TestCase):
	def test_tokens_handle_repeated_messages_and_empty_pages(
		self,
	) -> None:
		name = 'cmd/i/plugin/stdout'
		aws = FakeAws(
			{
				'describe-log-streams': [stream_page(name)] * 2,
				'get-log-events': [
					events('a'),
					events('b', 'same', 'same'),
					events('b'),
					events('b'),
				],
			}
		)
		watch = CloudWatch(aws, 'group', 'cmd', 'i')
		with contextlib.redirect_stdout(io.StringIO()) as logs:
			watch.poll(None)
			watch.poll(None)
		self.assertEqual(
			logs.getvalue().count('[cloudwatch/stdout] same'), 2
		)
		self.assertEqual(watch.tokens[name], 'b')
		self.assertEqual(aws.calls[-1][2]['next_token'], 'b')
		self.assertTrue(aws.calls[-1][2]['start_from_head'])

	def test_discovery_is_paginated_and_scoped_to_command(
		self,
	) -> None:
		name = 'cmd/i/plugin/stderr'
		aws = FakeAws(
			{
				'describe-log-streams': [
					{'logStreams': [], 'nextToken': 'page2'},
					{
						'logStreams': [
							{'logStreamName': 'x/i/p/stdout'},
							{'logStreamName': name},
						]
					},
				],
				'get-log-events': [events('a', 'error'), events('a')],
			}
		)
		watch = CloudWatch(aws, 'group', 'cmd', 'i')
		with contextlib.redirect_stdout(io.StringIO()) as logs:
			watch.poll(None)
			watch.poll(None)
		self.assertEqual(aws.calls[1][2]['next_token'], 'page2')
		self.assertEqual(set(watch.tokens), {name})
		self.assertIn('[cloudwatch/stderr] error', logs.getvalue())

	def test_missing_group_does_not_disable_streaming(self) -> None:
		aws = FakeAws(
			{
				'describe-log-streams': [
					AwsError('ResourceNotFoundException', 'not yet')
				]
			}
		)
		watch = CloudWatch(aws, 'group', 'cmd', 'i')
		watch.poll(None)
		self.assertFalse(watch.disabled)

	def test_busy_stream_has_a_per_poll_budget(self) -> None:
		name = 'cmd/i/plugin/stdout'
		aws = FakeAws(
			{
				'describe-log-streams': [stream_page(name)],
				'get-log-events': [
					events(str(i), 'line') for i in range(10)
				],
			}
		)
		watch = CloudWatch(aws, 'group', 'cmd', 'i')
		with contextlib.redirect_stdout(io.StringIO()):
			watch.poll(None)
		self.assertFalse(watch.caught_up)
		self.assertEqual(len(aws.calls), 11)

	def test_remote_lines_cannot_issue_workflow_commands(
		self,
	) -> None:
		with contextlib.redirect_stdout(io.StringIO()) as logs:
			remote_log(
				'cloudwatch/stdout',
				'first\n::error::fake\n::add-mask::value',
			)
		self.assertTrue(
			all(
				line.startswith('[cloudwatch/stdout] ')
				for line in logs.getvalue().splitlines()
			)
		)
