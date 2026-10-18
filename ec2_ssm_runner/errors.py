"""Execution errors and retryable AWS error codes."""

from __future__ import annotations

TRANSIENT = {
	'Throttling',
	'ThrottlingException',
	'TooManyRequestsException',
	'RequestLimitExceeded',
	'InternalServerError',
	'InternalServerErrorException',
	'ServiceUnavailable',
	'ServiceUnavailableException',
	'RequestTimeout',
	'RequestTimeoutException',
	'ClientTimeout',
	'ConnectionError',
}


class AwsError(RuntimeError):
	def __init__(self, code: str, message: str) -> None:
		self.code = code
		super().__init__(f'{code}: {message}')


class DeadlineExceeded(RuntimeError):
	pass


class Interrupted(RuntimeError):
	def __init__(self, signum: int) -> None:
		self.signum = signum
		super().__init__(f'Received signal {signum}')
