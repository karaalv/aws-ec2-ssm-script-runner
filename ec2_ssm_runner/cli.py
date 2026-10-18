"""Validate the environment and install interruption handlers."""

from __future__ import annotations

import os
import shutil
import signal
from types import FrameType
from typing import NoReturn

from .aws.cli import AwsCli
from .config import Config
from .errors import Interrupted
from .logs.console import log
from .runner import Runner


def main() -> int:
	try:
		config = Config.from_env(os.environ)
		if not shutil.which('aws'):
			raise ValueError('aws CLI not found on runner')
	except ValueError as exc:
		log(f'[error] {exc}')
		return 2

	def interrupted(
		signum: int,
		_frame: FrameType | None,
	) -> NoReturn:
		# Finish remote cancellation before accepting more signals.
		signal.signal(signal.SIGINT, signal.SIG_IGN)
		signal.signal(signal.SIGTERM, signal.SIG_IGN)
		raise Interrupted(signum)

	signal.signal(signal.SIGINT, interrupted)
	signal.signal(signal.SIGTERM, interrupted)
	return Runner(config, AwsCli()).run()
