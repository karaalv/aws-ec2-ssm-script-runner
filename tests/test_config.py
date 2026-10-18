import unittest

from tests.support import (
	config,
)


class ValidationTests(unittest.TestCase):
	def test_bad_inputs_fail_before_any_aws_call(self):
		for values in (
			{'INPUT_POLL_INTERVAL': '0'},
			{'INPUT_TIMEOUT': '-1'},
			{'INPUT_TIMEOUT': '172801'},
			{'INPUT_CLOUDWATCH_LOGS': 'maybe'},
			{'INPUT_CLOUDWATCH_DRAIN': '301'},
			{'INPUT_SCRIPT_NAME': '../file'},
			{'INPUT_SCRIPT_NAME': '.'},
			{'INPUT_ENV_VARS': 'bad key=secret'},
			{'INPUT_ENV_VARS': 'SSM_RUNNER_DIR=/important'},
			{'INPUT_SCRIPT_LOCATION': 'bad\nkey'},
			{'AWS_REGION': '', 'AWS_DEFAULT_REGION': ''},
		):
			with (
				self.subTest(values=values),
				self.assertRaises(ValueError),
			):
				config(**values)

	def test_values_are_preserved_and_errors_hide_secrets(self):
		cfg = config(
			INPUT_ENV_VARS="# comment\nKEY=  a=b'c  \nEMPTY="
		)
		self.assertEqual(
			cfg.exports, [('KEY', "  a=b'c  "), ('EMPTY', '')]
		)
		with self.assertRaises(ValueError) as error:
			config(INPUT_ENV_VARS='bad key=secret-value')
		self.assertNotIn('secret-value', str(error.exception))
