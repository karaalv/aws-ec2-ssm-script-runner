import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import (
	BASE_ENV,
	make_aws,
)


class EntrypointTests(unittest.TestCase):
	def test_bash_entrypoint_with_fake_aws_process(self):
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			make_aws(root)
			output = root / 'outputs'
			env = dict(os.environ, **BASE_ENV)
			env.update(
				PATH=f'{root}:' + os.environ['PATH'],
				GITHUB_OUTPUT=str(output),
				INPUT_CLOUDWATCH_LOGS='false',
				INPUT_ENV_VARS='',
			)
			result = subprocess.run(
				[
					'bash',
					str(
						Path(__file__).resolve().parents[1]
						/ 'scripts'
						/ 'run.sh'
					),
				],
				text=True,
				capture_output=True,
				env=env,
				timeout=10,
			)
			self.assertEqual(
				result.returncode, 0, result.stdout + result.stderr
			)
			self.assertIn(
				'[ssm/stdout] visible output', result.stdout
			)
			self.assertIn('[ssm/stderr] visible error', result.stdout)
			self.assertIn('command-id=cmd', output.read_text())
