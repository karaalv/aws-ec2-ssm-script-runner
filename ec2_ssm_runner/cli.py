"""Validate the environment and install interruption handlers."""

import os
import shutil
import signal

from .aws.cli import AwsCli
from .config import Config
from .errors import Interrupted
from .logs.console import log
from .runner import Runner


def main():
	try:
		config = Config.from_env(os.environ)
		if not shutil.which('aws'):
			raise ValueError('aws CLI not found on runner')
	except ValueError as exc:
		log(f'[error] {exc}')
		return 2

	def interrupted(signum, _frame):
		# Finish remote cancellation before accepting more signals.
		signal.signal(signal.SIGINT, signal.SIG_IGN)
		signal.signal(signal.SIGTERM, signal.SIG_IGN)
		raise Interrupted(signum)

	signal.signal(signal.SIGINT, interrupted)
	signal.signal(signal.SIGTERM, interrupted)
	return Runner(config, AwsCli()).run()
