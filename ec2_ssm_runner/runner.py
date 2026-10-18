"""Coordinate submission, monitoring, logging and failure cleanup."""

import time

from . import github
from .aws.ssm import ACTIVE, TERMINAL, SsmClient
from .errors import TRANSIENT, AwsError, DeadlineExceeded, Interrupted
from .logs.cloudwatch import CloudWatch
from .logs.console import log
from .logs.ssm import SsmOutput


class Runner:
	def __init__(self, config, aws):
		self.config = config
		self.aws = aws
		self.ssm = SsmClient(aws)
		self.command_id = None
		self.status = 'Pending'
		self.exit_code = ''
		self.snapshot = {}
		self.ssm_logs = SsmOutput()
		self.cloudwatch = None

	def run(self):
		try:
			self._submit()
			self._monitor()
			return self._finish()
		except (RuntimeError, ValueError, OSError) as exc:
			return self._handle_failure(exc)

	def _submit(self):
		self.config.instance_id = self.ssm.resolve_instance(
			self.config.instance_id,
			self.config.instance_name,
		)
		github.mask_values(self.config.exports)
		log(f'Sending SSM command to {self.config.instance_id}')
		self.command_id = self.ssm.submit(self.config)
		github.output(**{'command-id': self.command_id})
		log(f'Command ID: {self.command_id}')
		if self.config.cloudwatch:
			self.cloudwatch = CloudWatch(
				self.aws,
				self.config.group,
				self.command_id,
				self.config.instance_id,
			)
			log(f'CloudWatch log group: {self.config.group}')

	def _monitor(self):
		deadline = time.monotonic() + self.config.timeout
		failures = 0
		last_status = None
		last_heartbeat = time.monotonic()
		while True:
			if time.monotonic() >= deadline:
				raise DeadlineExceeded(
					f'Timed out after {self.config.timeout}s'
				)
			try:
				self.snapshot = self.ssm.invocation(
					self.command_id,
					self.config.instance_id,
					deadline,
				)
				failures = 0
				self.status = self.snapshot['Status']
				self.exit_code = self.snapshot.get('ResponseCode', '')
				now = time.monotonic()
				if (
					self.status != last_status
					or now - last_heartbeat >= 30
				):
					self._heartbeat(deadline, now)
					last_status = self.status
					last_heartbeat = now
				if self.status in TERMINAL:
					return
				self._ssm_output()
				if self.status not in ACTIVE:
					raise RuntimeError(
						f'Unexpected SSM status: {self.status}'
					)
			except AwsError as exc:
				if exc.code != 'InvocationDoesNotExist':
					if exc.code not in TRANSIENT:
						raise
					failures += 1
					log(
						f'[warning] SSM polling attempt '
						f'{failures}/5: {exc}'
					)
					if failures >= 5:
						raise
			if self.cloudwatch:
				self.cloudwatch.poll(deadline)
			time.sleep(
				max(
					0,
					min(
						self.config.poll, deadline - time.monotonic()
					),
				)
			)

	def _heartbeat(self, deadline, now):
		elapsed = int(self.config.timeout - (deadline - now))
		details = self.snapshot.get('StatusDetails', self.status)
		log(
			f'SSM status: {self.status} ({details}); '
			f'elapsed {elapsed}s'
		)

	def _ssm_output(self, final=False):
		if final or not self.cloudwatch or self.cloudwatch.disabled:
			self.ssm_logs.emit(self.snapshot, final=final)

	def _finish(self):
		github.output(
			status=self.status,
			**{'exit-code': self.exit_code},
		)
		if self.cloudwatch:
			self.cloudwatch.drain(
				self.config.drain,
				self.config.poll,
			)
			log(
				'SSM backup output (may repeat CloudWatch output; '
				'SSM character limits apply):'
			)
		self._ssm_output(final=True)
		log(
			f'Final SSM status: {self.status}; '
			f'exit code: {self.exit_code}'
		)
		return (
			0
			if self.status == 'Success' and self.exit_code == 0
			else 1
		)

	def _cancel(self):
		if self.command_id and self.status not in TERMINAL:
			log(
				'Requesting cancellation of SSM command '
				f'{self.command_id}'
			)
			try:
				self.ssm.cancel(
					self.command_id,
					self.config.instance_id,
					time.monotonic() + 10,
				)
			except (AwsError, DeadlineExceeded) as exc:
				log(
					'[warning] Cancellation could not be '
					f'confirmed: {exc}'
				)

	def _final_diagnostics(self):
		# Preserve the last known snapshot if the final request fails.
		try:
			self.snapshot = self.ssm.invocation(
				self.command_id,
				self.config.instance_id,
				time.monotonic() + 5,
			)
		except (AwsError, DeadlineExceeded):
			pass
		if self.cloudwatch:
			try:
				self.cloudwatch.poll(time.monotonic() + 5)
			except DeadlineExceeded:
				pass
		log('SSM backup output after monitoring stopped:')
		self._ssm_output(final=True)

	def _handle_failure(self, error):
		log(f'[error] {error}')
		if (
			not self.command_id
			and isinstance(error, AwsError)
			and error.code in TRANSIENT
		):
			log(
				'[warning] Submission result may be unknown; '
				'check SSM before rerunning'
			)
		self._cancel()
		if self.command_id:
			self._final_diagnostics()
			outcome = 'MonitoringFailed'
			if isinstance(error, DeadlineExceeded):
				outcome = 'TimedOut'
			elif isinstance(error, Interrupted):
				outcome = 'Cancelled'
			github.output(
				status=outcome,
				**{
					'exit-code': self.snapshot.get('ResponseCode', '')
				},
			)
		if isinstance(error, Interrupted):
			return 128 + error.signum
		return 1
