"""Incremental CloudWatch output collection with SSM fallback."""

from __future__ import annotations

import time
from typing import cast

from ..aws.types import (
	AwsTransport,
	LogEventQuery,
	LogEventsPage,
	LogStreamQuery,
	LogStreamsPage,
)
from ..errors import TRANSIENT, AwsError, DeadlineExceeded
from .console import log, remote_log


class CloudWatch:
	def __init__(
		self,
		aws: AwsTransport,
		group: str,
		command_id: str,
		instance_id: str,
	) -> None:
		self.aws = aws
		self.group = group
		self.prefix = f'{command_id}/{instance_id}/'
		self.tokens: dict[str, str | None] = {}
		self.discovery_token: str | None = None
		self.disabled = False
		self.warned: set[str] = set()
		self.events = 0
		self.caught_up = True

	def poll(self, deadline: float | None) -> None:
		if self.disabled:
			return
		try:
			self._poll(deadline)
		except AwsError as exc:
			if exc.code == 'ResourceNotFoundException':
				# Streams may not exist until output arrives.
				return
			if exc.code not in self.warned:
				log(
					f'[warning] CloudWatch {exc}; '
					'SSM output remains available as backup'
				)
				self.warned.add(exc.code)
			if exc.code not in TRANSIENT:
				self.disabled = True
				log(
					'[warning] CloudWatch streaming disabled; '
					'using SSM output'
				)

	def _poll(self, deadline: float | None) -> None:
		if len(self.tokens) < 2:
			self._discover_streams(deadline)
		self.caught_up = True
		for name in list(self.tokens):
			if not self._read_stream(name, deadline):
				self.caught_up = False

	def _discover_streams(self, deadline: float | None) -> None:
		# Discover names without assuming the SSM plugin identifier.
		params: LogStreamQuery = dict(
			log_group_name=self.group,
			log_stream_name_prefix=self.prefix,
		)
		if self.discovery_token:
			params['next_token'] = self.discovery_token
		page = cast(
			LogStreamsPage,
			self.aws.call(
				'logs',
				'describe-log-streams',
				deadline=deadline,
				**params,
			),
		)
		self.discovery_token = page.get('nextToken')
		for stream in page.get('logStreams', []):
			name = stream['logStreamName']
			channel = name.rsplit('/', 1)[-1]
			if name.startswith(self.prefix) and channel in {
				'stdout',
				'stderr',
			}:
				self.tokens.setdefault(name, None)

	def _read_stream(
		self,
		name: str,
		deadline: float | None,
	) -> bool:
		token = self.tokens[name]
		# Bound each poll so busy streams cannot starve status checks.
		for _ in range(10):
			params: LogEventQuery = dict(
				log_group_name=self.group,
				log_stream_name=name,
				start_from_head=True,
			)
			if token:
				params['next_token'] = token
			page = cast(
				LogEventsPage,
				self.aws.call(
					'logs',
					'get-log-events',
					deadline=deadline,
					**params,
				),
			)
			for event in page.get('events', []):
				remote_log(
					'cloudwatch/' + name.rsplit('/', 1)[-1],
					event['message'],
				)
				self.events += 1
			new_token = page['nextForwardToken']
			self.tokens[name] = new_token
			if new_token == token:
				return True
			token = new_token
		return False

	def drain(self, seconds: int, poll: int) -> None:
		if self.disabled or seconds == 0:
			return
		log(f'Waiting up to {seconds}s for delayed CloudWatch output')
		deadline = time.monotonic() + seconds
		try:
			while time.monotonic() < deadline and not self.disabled:
				self.poll(deadline)
				time.sleep(
					max(0, min(poll, deadline - time.monotonic()))
				)
		except DeadlineExceeded:
			pass
		if not self.events:
			log(
				'[warning] No CloudWatch events received; '
				'use the SSM backup below'
			)
		if not self.caught_up:
			log(
				'[warning] CloudWatch output is still catching up; '
				'remaining logs are in CloudWatch'
			)
