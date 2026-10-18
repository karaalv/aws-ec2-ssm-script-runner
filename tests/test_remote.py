from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ec2_ssm_runner.remote import remote_commands
from tests.support import (
	config,
	make_aws,
)


class RemoteExecutionTests(unittest.TestCase):
	def test_quoting_cleanup_and_exit_codes_with_real_shell(
		self,
	) -> None:
		for exit_code in (0, 7):
			with (
				self.subTest(exit_code=exit_code),
				tempfile.TemporaryDirectory() as directory,
			):
				root = Path(directory)
				make_aws(root)
				temp = root / 'temp'
				temp.mkdir()
				marker = root / 'injected'
				cfg = config(
					INPUT_SCRIPT_NAME="run ' $(touch injected).sh",
					INPUT_SCRIPT_LOCATION=(
						f"deploy/a ' $(touch {marker}).sh"
					),
					INPUT_ENV_VARS=f"VALUE=  a'b=$(touch {marker})  ",
				)
				script = '\n'.join(remote_commands(cfg))
				result = subprocess.run(
					['/bin/sh', '-c', script],
					cwd=root,
					text=True,
					capture_output=True,
					env=dict(
						os.environ,
						PATH=f'{root}:' + os.environ['PATH'],
						TMPDIR=str(temp),
						MOCK_SCRIPT=(
							'#!/bin/sh\n'
							'printf "<%s>\\n" "$VALUE"\n'
							f'exit {exit_code}\n'
						),
					),
				)
				self.assertEqual(
					result.returncode, exit_code, result.stderr
				)
				self.assertIn(
					f"<  a'b=$(touch {marker})  >", result.stdout
				)
				self.assertFalse(marker.exists())
				self.assertEqual(list(temp.iterdir()), [])
				self.assertNotIn('export VALUE', result.stderr)
