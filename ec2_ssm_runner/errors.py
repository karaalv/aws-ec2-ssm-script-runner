"""Execution errors and retryable AWS error codes."""

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
	def __init__(self, code, message):
		self.code = code
		super().__init__(f'{code}: {message}')


class DeadlineExceeded(RuntimeError):
	pass


class Interrupted(RuntimeError):
	def __init__(self, signum):
		self.signum = signum
		super().__init__(f'Received signal {signum}')
