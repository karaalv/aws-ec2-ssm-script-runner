"""AWS response shapes and the injectable CLI transport contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypedDict


class InvocationOutput(TypedDict, total=False):
	ResponseCode: int
	StandardOutputContent: str
	StandardErrorContent: str
	StatusDetails: str


class Invocation(InvocationOutput):
	Status: str


class Instance(TypedDict):
	InstanceId: str


class Reservation(TypedDict, total=False):
	Instances: list[Instance]


class InstancesPage(TypedDict, total=False):
	Reservations: list[Reservation]
	NextToken: str


class Command(TypedDict):
	CommandId: str


class Submission(TypedDict):
	Command: Command


class LogStream(TypedDict):
	logStreamName: str


class LogStreamsPage(TypedDict, total=False):
	logStreams: list[LogStream]
	nextToken: str


class LogEvent(TypedDict):
	message: str


class LogEventsPage(TypedDict):
	events: list[LogEvent]
	nextForwardToken: str


class AwsTransport(Protocol):
	def call(
		self,
		service: str,
		operation: str,
		*,
		payload: Mapping[str, object] | None = None,
		deadline: float | None = None,
		**params: str | int | bool | None,
	) -> dict[str, Any]:
		"""Return a JSON object, narrowed by each service adapter."""
		...
