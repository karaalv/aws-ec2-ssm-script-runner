"""Direct SSM output snapshots, duplicate suppression and limits."""

from __future__ import annotations

from ..aws.types import InvocationOutput
from .console import log, remote_log


class SsmOutput:
	def __init__(self) -> None:
		self.seen = {'stdout': '', 'stderr': ''}

	def emit(
		self,
		snapshot: InvocationOutput,
		final: bool = False,
	) -> None:
		for stream, current in (
			('stdout', snapshot.get('StandardOutputContent') or ''),
			('stderr', snapshot.get('StandardErrorContent') or ''),
		):
			previous = self.seen[stream]
			delta = (
				current[len(previous) :]
				if current.startswith(previous)
				else current
			)
			if delta:
				remote_log('ssm/' + stream, delta)
			self.seen[stream] = current
			limit = 24000 if stream == 'stdout' else 8000
			if final and len(current) >= limit:
				log(
					f'[warning] SSM {stream} reached its '
					f'{limit}-character limit; '
					'full logs require CloudWatch'
				)
