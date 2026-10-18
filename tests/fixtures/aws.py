"""Fake AWS executable for entry-point and remote shell tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
	if sys.argv[1:4] == ['s3', 'cp', '--']:
		Path(sys.argv[-1]).write_text(os.environ['MOCK_SCRIPT'])
		return
	operation = sys.argv[2]
	if operation == 'send-command':
		index = sys.argv.index('--cli-input-json') + 1
		path = Path(sys.argv[index].removeprefix('file://'))
		payload = json.loads(path.read_text())
		assert payload['Parameters']['executionTimeout'] == ['10']
		print(json.dumps({'Command': {'CommandId': 'cmd'}}))
	elif operation == 'get-command-invocation':
		print(
			json.dumps(
				{
					'Status': 'Success',
					'ResponseCode': 0,
					'StandardOutputContent': 'visible output\n',
					'StandardErrorContent': 'visible error\n',
				}
			)
		)
	else:
		raise SystemExit('Unexpected AWS operation')


if __name__ == '__main__':
	main()
