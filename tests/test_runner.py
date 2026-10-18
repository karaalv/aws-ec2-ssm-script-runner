import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import runner


BASE_ENV = {
    'AWS_REGION': 'eu-west-1',
    'INPUT_INSTANCE_ID': 'i-example',
    'INPUT_BUCKET_NAME': 'example-scripts',
    'INPUT_SCRIPT_LOCATION': 'deploy/run.sh',
    'INPUT_POLL_INTERVAL': '1',
    'INPUT_TIMEOUT': '10',
}


def config(**values):
    return runner.Config.from_env(dict(BASE_ENV, **values))


def snapshot(status='Success', code=0, stdout='', stderr=''):
    return dict(Status=status, ResponseCode=code, StandardOutputContent=stdout,
                StandardErrorContent=stderr)


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeAws:
    def __init__(self, responses):
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls = []

    def call(self, service, operation, **kwargs):
        self.calls.append((service, operation, kwargs))
        values = self.responses.get(operation)
        if not values:
            raise AssertionError(f'Unexpected AWS call: {operation} {kwargs}')
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def stream_page(name):
    return {'logStreams': [{'logStreamName': name}]}


def events(token, *messages):
    return {'nextForwardToken': token, 'events': [{'message': m} for m in messages]}


class RunnerTests(unittest.TestCase):
    def run_action(self, aws, cfg=None):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'outputs'
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                    patch.object(runner.time, 'monotonic', clock.monotonic), \
                    patch.object(runner.time, 'sleep', clock.sleep), \
                    contextlib.redirect_stdout(io.StringIO()) as logs:
                result = runner.Runner(cfg or config(), aws).run()
            return result, logs.getvalue(), output.read_text() if output.exists() else ''

    def test_poll_once_per_cycle_and_preserve_both_streams(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot('InProgress', -1),
                                                  snapshot(stdout='hello\n', stderr='warning\n')]})
        result, logs, output = self.run_action(aws)
        self.assertEqual(result, 0)
        self.assertIn('[ssm/stdout] hello', logs)
        self.assertIn('[ssm/stderr] warning', logs)
        self.assertEqual(sum(c[1] == 'get-command-invocation' for c in aws.calls), 2)
        self.assertTrue(output.startswith('command-id=cmd\n'))
        self.assertIn('status=Success\nexit-code=0', output)
        payload = aws.calls[0][2]['payload']
        self.assertNotIn('CloudWatchOutputConfig', payload)
        self.assertEqual(payload['Parameters']['executionTimeout'], ['10'])

    def test_ssm_prints_only_new_text(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot('InProgress', -1, 'first\n'),
                                                  snapshot(stdout='first\nsecond\n')]})
        _, logs, _ = self.run_action(aws)
        self.assertEqual(logs.count('[ssm/stdout] first'), 1)
        self.assertEqual(logs.count('[ssm/stdout] second'), 1)

    def test_propagation_and_throttling_are_retried(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [runner.AwsError('InvocationDoesNotExist', 'not yet'),
                           runner.AwsError('ThrottlingException', 'slow down'), snapshot()]})
        result, logs, _ = self.run_action(aws)
        self.assertEqual(result, 0)
        self.assertIn('ThrottlingException', logs)

    def test_permission_failure_is_visible_and_cancels(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [runner.AwsError('AccessDeniedException', 'denied'),
                                                  snapshot('InProgress', -1, 'last output')],
                       'cancel-command': [{}]})
        result, logs, output = self.run_action(aws)
        self.assertEqual(result, 1)
        self.assertIn('AccessDeniedException', logs)
        self.assertIn('[ssm/stdout] last output', logs)
        self.assertIn('status=MonitoringFailed', output)
        self.assertEqual(sum(c[1] == 'cancel-command' for c in aws.calls), 1)

    def test_timeout_attempts_cancellation(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot('InProgress', -1)] * 3,
                       'cancel-command': [{}]})
        result, _, output = self.run_action(aws, config(INPUT_TIMEOUT='2'))
        self.assertEqual(result, 1)
        self.assertIn('status=TimedOut', output)
        self.assertEqual(sum(c[1] == 'cancel-command' for c in aws.calls), 1)

    def test_interruption_cancels_and_preserves_output(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [runner.Interrupted(signal.SIGTERM),
                                                  snapshot('Cancelled', -1, 'before interruption')],
                       'cancel-command': [{}]})
        result, logs, output = self.run_action(aws)
        self.assertEqual(result, 143)
        self.assertIn('[ssm/stdout] before interruption', logs)
        self.assertIn('status=Cancelled', output)

    def test_repeated_transient_errors_are_bounded_and_cancel_failure_is_visible(self):
        error = runner.AwsError('ThrottlingException', 'slow down')
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [error] * 5 + [snapshot('InProgress', -1)],
                       'cancel-command': [runner.AwsError('AccessDeniedException', 'cannot cancel')]})
        result, logs, output = self.run_action(aws)
        self.assertEqual(result, 1)
        self.assertIn('attempt 5/5', logs)
        self.assertIn('Cancellation could not be confirmed', logs)
        self.assertIn('status=MonitoringFailed', output)

    def test_remote_failure_and_nonzero_success_fail_action(self):
        for status in ('Failed', 'Success'):
            with self.subTest(status=status):
                aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                               'get-command-invocation': [snapshot(status, 7, stderr='script failed')]})
                result, logs, output = self.run_action(aws)
                self.assertEqual(result, 1)
                self.assertIn('[ssm/stderr] script failed', logs)
                self.assertIn('exit-code=7', output)

    def test_cancelling_is_not_a_terminal_state(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot('Cancelling', -1), snapshot('Cancelled', -1)]})
        result, logs, _ = self.run_action(aws)
        self.assertEqual(result, 1)
        self.assertIn('SSM status: Cancelled', logs)

    def test_submission_is_not_retried_on_ambiguous_failure(self):
        aws = FakeAws({'send-command': [runner.AwsError('ClientTimeout', 'timeout')]})
        result, logs, output = self.run_action(aws)
        self.assertEqual(result, 1)
        self.assertEqual(len(aws.calls), 1)
        self.assertEqual(output, '')
        self.assertIn('check SSM before rerunning', logs)

    def test_cloudwatch_denial_falls_back_without_failing_script(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot('InProgress', -1), snapshot(stdout='backup')],
                       'describe-log-streams': [runner.AwsError('AccessDeniedException', 'denied')]})
        result, logs, _ = self.run_action(aws, config(INPUT_CLOUDWATCH_LOGS='true'))
        self.assertEqual(result, 0)
        self.assertIn('streaming disabled', logs)
        self.assertIn('[ssm/stdout] backup', logs)
        self.assertTrue(aws.calls[0][2]['payload']['CloudWatchOutputConfig']['CloudWatchOutputEnabled'])

    def test_late_cloudwatch_events_are_drained_and_ssm_backup_is_kept(self):
        name = 'cmd/i-example/plugin/stdout'
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot(stdout='complete')],
                       'describe-log-streams': [stream_page(name)] * 3,
                       'get-log-events': [events('1'), events('1'), events('2', 'late'),
                                          events('2'), events('2')]})
        result, logs, _ = self.run_action(aws, config(INPUT_CLOUDWATCH_LOGS='true', INPUT_CLOUDWATCH_DRAIN='3'))
        self.assertEqual(result, 0)
        self.assertEqual(logs.count('[cloudwatch/stdout] late'), 1)
        self.assertIn('[ssm/stdout] complete', logs)

    def test_ssm_truncation_is_reported(self):
        aws = FakeAws({'send-command': [{'Command': {'CommandId': 'cmd'}}],
                       'get-command-invocation': [snapshot(stdout='x' * 24000)]})
        _, logs, _ = self.run_action(aws)
        self.assertIn('24000-character limit', logs)

    def test_instance_name_resolution_checks_every_page(self):
        aws = FakeAws({'describe-instances': [
            {'Reservations': [{'Instances': [{'InstanceId': 'i-one'}]}], 'NextToken': 'next'},
            {'Reservations': [{'Instances': [{'InstanceId': 'i-two'}]}]},
        ]})
        result, logs, _ = self.run_action(aws, config(INPUT_INSTANCE_ID='', INPUT_INSTANCE_NAME='shared'))
        self.assertEqual(result, 1)
        self.assertIn('matched 2', logs)
        self.assertEqual(len(aws.calls), 2)


class CloudWatchTests(unittest.TestCase):
    def test_tokens_preserve_identical_events_and_empty_intermediate_pages(self):
        name = 'cmd/i/plugin/stdout'
        aws = FakeAws({'describe-log-streams': [stream_page(name)] * 2,
                       'get-log-events': [events('a'), events('b', 'same', 'same'),
                                          events('b'), events('b')]})
        watch = runner.CloudWatch(aws, 'group', 'cmd', 'i')
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            watch.poll(None)
            watch.poll(None)
        self.assertEqual(logs.getvalue().count('[cloudwatch/stdout] same'), 2)
        self.assertEqual(watch.tokens[name], 'b')
        self.assertEqual(aws.calls[-1][2]['next_token'], 'b')
        self.assertTrue(aws.calls[-1][2]['start_from_head'])

    def test_stream_discovery_is_paginated_and_scoped_to_this_command(self):
        name = 'cmd/i/plugin/stderr'
        aws = FakeAws({'describe-log-streams': [
            {'logStreams': [], 'nextToken': 'page2'},
            {'logStreams': [{'logStreamName': 'other/i/plugin/stdout'}, {'logStreamName': name}]},
        ], 'get-log-events': [events('a', 'error'), events('a')]})
        watch = runner.CloudWatch(aws, 'group', 'cmd', 'i')
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            watch.poll(None)
            watch.poll(None)
        self.assertEqual(aws.calls[1][2]['next_token'], 'page2')
        self.assertEqual(set(watch.tokens), {name})
        self.assertIn('[cloudwatch/stderr] error', logs.getvalue())

    def test_missing_group_is_expected_and_does_not_disable_streaming(self):
        aws = FakeAws({'describe-log-streams': [runner.AwsError('ResourceNotFoundException', 'not yet')]})
        watch = runner.CloudWatch(aws, 'group', 'cmd', 'i')
        watch.poll(None)
        self.assertFalse(watch.disabled)

    def test_busy_stream_has_a_per_poll_budget(self):
        name = 'cmd/i/plugin/stdout'
        aws = FakeAws({'describe-log-streams': [stream_page(name)],
                       'get-log-events': [events(str(i), 'line') for i in range(10)]})
        watch = runner.CloudWatch(aws, 'group', 'cmd', 'i')
        with contextlib.redirect_stdout(io.StringIO()):
            watch.poll(None)
        self.assertFalse(watch.caught_up)
        self.assertEqual(len(aws.calls), 11)

    def test_remote_lines_cannot_issue_workflow_commands(self):
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            runner.remote_log('cloudwatch/stdout', 'first\n::error::fake\n::add-mask::value')
        self.assertTrue(all(line.startswith('[cloudwatch/stdout] ') for line in logs.getvalue().splitlines()))


class EntrypointTests(unittest.TestCase):
    def test_bash_entrypoint_with_fake_aws_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / 'aws'
            binary.write_text(f'#!{sys.executable}\n' + r'''import json, pathlib, sys
operation = sys.argv[2]
if operation == 'send-command':
    payload = json.loads(pathlib.Path(sys.argv[sys.argv.index('--cli-input-json') + 1][7:]).read_text())
    assert payload['Parameters']['executionTimeout'] == ['10']
    print(json.dumps({'Command': {'CommandId': 'cmd'}}))
elif operation == 'get-command-invocation':
    print(json.dumps({'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': 'visible output\n', 'StandardErrorContent': 'visible error\n'}))
else:
    raise SystemExit('Unexpected AWS operation')
''')
            binary.chmod(0o755)
            output = root / 'outputs'
            env = dict(os.environ, **BASE_ENV)
            env.update(PATH=f'{root}:' + os.environ['PATH'], GITHUB_OUTPUT=str(output),
                       INPUT_CLOUDWATCH_LOGS='false', INPUT_ENV_VARS='')
            result = subprocess.run(['bash', str(Path(runner.__file__).with_name('run.sh'))],
                                    text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('[ssm/stdout] visible output', result.stdout)
            self.assertIn('[ssm/stderr] visible error', result.stdout)
            self.assertIn('command-id=cmd', output.read_text())


class ValidationTests(unittest.TestCase):
    def test_bad_inputs_fail_before_any_aws_call(self):
        for values in ({'INPUT_POLL_INTERVAL': '0'}, {'INPUT_TIMEOUT': '-1'},
                       {'INPUT_TIMEOUT': '172801'}, {'INPUT_CLOUDWATCH_LOGS': 'maybe'},
                       {'INPUT_CLOUDWATCH_DRAIN': '301'}, {'INPUT_SCRIPT_NAME': '../file'},
                       {'INPUT_SCRIPT_NAME': '.'}, {'INPUT_ENV_VARS': 'bad key=secret'},
                       {'INPUT_ENV_VARS': 'SSM_RUNNER_DIR=/important'},
                       {'INPUT_SCRIPT_LOCATION': 'bad\nkey'}, {'AWS_REGION': '', 'AWS_DEFAULT_REGION': ''}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)

    def test_values_are_preserved_without_echoing_invalid_secrets(self):
        cfg = config(INPUT_ENV_VARS="# comment\nKEY=  a=b'c  \nEMPTY=")
        self.assertEqual(cfg.exports, [('KEY', "  a=b'c  "), ('EMPTY', '')])
        with self.assertRaises(ValueError) as error:
            config(INPUT_ENV_VARS='bad key=secret-value')
        self.assertNotIn('secret-value', str(error.exception))


class RemoteExecutionTests(unittest.TestCase):
    def test_quoting_cleanup_and_exit_codes_with_real_shell(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root / 'aws'
                binary.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, sys
assert sys.argv[1:4] == ['s3', 'cp', '--']
pathlib.Path(sys.argv[-1]).write_text(os.environ['MOCK_SCRIPT'])
''')
                binary.chmod(0o755)
                temp = root / 'temp'
                temp.mkdir()
                marker = root / 'injected'
                cfg = config(INPUT_SCRIPT_NAME="run ' $(touch injected).sh",
                             INPUT_SCRIPT_LOCATION=f"deploy/a ' $(touch {marker}).sh",
                             INPUT_ENV_VARS=f"VALUE=  a'b=$(touch {marker})  ")
                script = '\n'.join(runner.remote_commands(cfg))
                result = subprocess.run(['/bin/sh', '-c', script], cwd=root, text=True,
                                        capture_output=True, env=dict(os.environ,
                                            PATH=f'{root}:' + os.environ['PATH'], TMPDIR=str(temp),
                                            MOCK_SCRIPT=f'#!/bin/sh\nprintf "<%s>\\n" "$VALUE"\nexit {exit_code}\n'))
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertIn(f"<  a'b=$(touch {marker})  >", result.stdout)
                self.assertFalse(marker.exists())
                self.assertEqual(list(temp.iterdir()), [])
                self.assertNotIn('export VALUE', result.stderr)


class AwsCliTests(unittest.TestCase):
    def test_payload_file_is_private_deleted_and_submission_has_no_automatic_retry(self):
        completed = subprocess.CompletedProcess([], 0, '{"Command": {"CommandId": "cmd"}}', '')
        recorded = {}
        def invoke(args, **kwargs):
            path = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
            recorded['path'] = path
            recorded['payload'] = json.loads(path.read_text())
            recorded['mode'] = path.stat().st_mode & 0o777
            return completed
        with patch.object(runner.subprocess, 'run', side_effect=invoke) as run:
            runner.AwsCli().call('ssm', 'send-command', payload={'secret': 'sensitive-value'})
        args, kwargs = run.call_args
        self.assertNotIn('sensitive-value', ' '.join(args[0]))
        self.assertEqual(recorded['payload'], {'secret': 'sensitive-value'})
        self.assertEqual(recorded['mode'], 0o600)
        self.assertFalse(recorded['path'].exists())
        self.assertEqual(kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
        self.assertEqual(kwargs['timeout'], 30)

    def test_cli_error_codes_are_classified(self):
        completed = subprocess.CompletedProcess([], 254, '', 'An error occurred (AccessDeniedException) when calling API: denied')
        with patch.object(runner.subprocess, 'run', return_value=completed), self.assertRaises(runner.AwsError) as error:
            runner.AwsCli().call('ssm', 'get-command-invocation')
        self.assertEqual(error.exception.code, 'AccessDeniedException')

    def test_client_calls_are_bounded_by_monitoring_deadline(self):
        with patch.object(runner.time, 'monotonic', return_value=10), \
                patch.object(runner.subprocess, 'run', side_effect=subprocess.TimeoutExpired('aws', 2)) as run, \
                self.assertRaises(runner.AwsError) as error:
            runner.AwsCli().call('ssm', 'get-command-invocation', deadline=12)
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertEqual(error.exception.code, 'ClientTimeout')


if __name__ == '__main__':
    unittest.main()
