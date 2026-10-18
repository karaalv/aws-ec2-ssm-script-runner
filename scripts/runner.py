#!/usr/bin/env python3
"""Run an S3 script through SSM and relay its output to GitHub Actions."""

from contextlib import ExitStack
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass


TERMINAL = {"Success", "Failed", "Cancelled", "TimedOut"}
ACTIVE = {"Pending", "InProgress", "Delayed", "Cancelling"}
TRANSIENT = {
    "Throttling", "ThrottlingException", "TooManyRequestsException",
    "RequestLimitExceeded", "InternalServerError", "InternalServerErrorException",
    "ServiceUnavailable", "ServiceUnavailableException", "RequestTimeout",
    "RequestTimeoutException", "ClientTimeout", "ConnectionError",
}


def log(message):
    print(message, flush=True)


def remote_log(source, message):
    # Prefix every line so remote output cannot issue GitHub workflow commands.
    for line in message.splitlines():
        log(f"[{source}] {line}")


def workflow_escape(value):
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class AwsError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


class DeadlineExceeded(RuntimeError):
    pass


class Interrupted(RuntimeError):
    def __init__(self, signum):
        self.signum = signum
        super().__init__(f"Received signal {signum}")


def integer(env, name, default, minimum=1, maximum=None):
    raw = env.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside the supported range")
    return value


@dataclass
class Config:
    instance_id: str
    instance_name: str
    bucket: str
    key: str
    filename: str
    exports: list
    poll: int
    timeout: int
    cloudwatch: bool
    group: str
    drain: int

    @classmethod
    def from_env(cls, env):
        bucket = env.get("INPUT_BUCKET_NAME", "")
        key = env.get("INPUT_SCRIPT_LOCATION", "")
        instance_id = env.get("INPUT_INSTANCE_ID", "")
        instance_name = env.get("INPUT_INSTANCE_NAME", "")
        if not bucket or not key:
            raise ValueError("bucket-name and script-location are required")
        if not instance_id and not instance_name:
            raise ValueError("instance-id or instance-name is required")
        if not env.get("AWS_REGION") and not env.get("AWS_DEFAULT_REGION"):
            raise ValueError("Set aws-region using configure-aws-credentials")
        filename = env.get("INPUT_SCRIPT_NAME") or key.rsplit("/", 1)[-1]
        if (filename in {"", ".", ".."} or "/" in filename or
                any(ord(c) < 32 or ord(c) == 127 for c in filename + key + bucket)):
            raise ValueError("Use a plain script filename and paths without control characters")
        exports = []
        for index, line in enumerate(env.get("INPUT_ENV_VARS", "").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if (not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    or name.startswith("SSM_RUNNER_")):
                raise ValueError(f"Invalid environment variable on line {index}")
            if "\0" in value:
                raise ValueError(f"Null character in environment variable on line {index}")
            exports.append((name, value))
        enabled = env.get("INPUT_CLOUDWATCH_LOGS", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("cloudwatch-logs must be true or false")
        group = env.get("INPUT_CLOUDWATCH_LOG_GROUP") or "/aws/ssm/AWS-RunShellScript"
        if not re.fullmatch(r"[.\-_/#A-Za-z0-9]{1,512}", group):
            raise ValueError("Invalid CloudWatch log group name")
        return cls(instance_id, instance_name, bucket, key, filename, exports,
                   integer(env, "INPUT_POLL_INTERVAL", 5),
                   integer(env, "INPUT_TIMEOUT", 1800, maximum=172800),
                   enabled == "true", group,
                   integer(env, "INPUT_CLOUDWATCH_DRAIN", 35, minimum=0, maximum=300))


def remote_commands(config):
    exports = [f"export {name}={shlex.quote(value)}" for name, value in config.exports]
    return [
        "set -eu",
        'SSM_RUNNER_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ssm-script.XXXXXXXX")',
        "trap 'result=$?; trap - EXIT; rm -rf -- \"$SSM_RUNNER_DIR\" || echo \"[runner] Cleanup failed\" >&2; exit \"$result\"' EXIT",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
        'echo "[runner] Downloading script from S3"',
        f'SSM_RUNNER_SCRIPT="$SSM_RUNNER_DIR"/{shlex.quote(config.filename)}',
        f'aws s3 cp -- {shlex.quote("s3://" + config.bucket + "/" + config.key)} "$SSM_RUNNER_SCRIPT"',
        'chmod +x "$SSM_RUNNER_SCRIPT"',
        "readonly SSM_RUNNER_DIR SSM_RUNNER_SCRIPT",
        *exports,
        'echo "[runner] Running script"',
        '"$SSM_RUNNER_SCRIPT"',
    ]


class AwsCli:
    def call(self, service, operation, *, payload=None, deadline=None, **params):
        remaining = 30 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("Monitoring deadline reached")
        args = ["aws", service, operation, "--output", "json", "--no-cli-pager",
                "--no-paginate", "--cli-connect-timeout", "5", "--cli-read-timeout", "20"]
        for key, value in params.items():
            option = "--" + key.replace("_", "-")
            args += [option] if value is True else [option, str(value)]
        attempts = "1" if operation == "send-command" else "2"
        env = dict(os.environ, AWS_PAGER="", AWS_MAX_ATTEMPTS=attempts, AWS_RETRY_MODE="standard")
        try:
            with ExitStack() as stack:
                if payload is not None:
                    # A mode-0600 temporary file keeps values out of process arguments.
                    file = stack.enter_context(tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", prefix="ssm-runner-", suffix=".json"))
                    json.dump(payload, file)
                    file.flush()
                    args += ["--cli-input-json", "file://" + file.name]
                result = subprocess.run(args, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, env=env,
                                        timeout=min(30, remaining))
        except subprocess.TimeoutExpired as exc:
            raise AwsError("ClientTimeout", "AWS CLI request timed out") from exc
        if result.returncode:
            match = re.search(r"An error occurred \(([^)]+)\)", result.stderr)
            code = match.group(1) if match else "CliError"
            if any(s in result.stderr for s in ("Could not connect to the endpoint", "Connection was closed", "Connect timeout", "Read timeout")):
                code = "ConnectionError"
            raise AwsError(code, result.stderr.strip())
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AwsError("InvalidResponse", "AWS CLI returned invalid JSON") from exc


class CloudWatch:
    def __init__(self, aws, group, command_id, instance_id):
        self.aws = aws
        self.group = group
        self.prefix = f"{command_id}/{instance_id}/"
        self.tokens = {}
        self.discovery_token = None
        self.disabled = False
        self.warned = set()
        self.events = 0
        self.caught_up = True

    def poll(self, deadline):
        if self.disabled:
            return
        try:
            self._poll(deadline)
        except AwsError as exc:
            if exc.code == "ResourceNotFoundException":
                return  # SSM creates groups and streams only when output arrives.
            if exc.code not in self.warned:
                log(f"[warning] CloudWatch {exc}; SSM output remains available as backup")
                self.warned.add(exc.code)
            if exc.code not in TRANSIENT:
                self.disabled = True
                log("[warning] CloudWatch streaming disabled; using SSM output")

    def _poll(self, deadline):
        # Discover names instead of assuming the SSM document's plugin identifier.
        if len(self.tokens) < 2:
            params = dict(log_group_name=self.group, log_stream_name_prefix=self.prefix)
            if self.discovery_token:
                params["next_token"] = self.discovery_token
            page = self.aws.call("logs", "describe-log-streams", deadline=deadline, **params)
            self.discovery_token = page.get("nextToken")
            for stream in page.get("logStreams", []):
                name = stream["logStreamName"]
                if name.startswith(self.prefix) and name.rsplit("/", 1)[-1] in {"stdout", "stderr"}:
                    self.tokens.setdefault(name, None)
        self.caught_up = True
        for name, token in list(self.tokens.items()):
            # Bound work per poll so busy streams cannot starve status checks.
            for _ in range(10):
                params = dict(log_group_name=self.group, log_stream_name=name, start_from_head=True)
                if token:
                    params["next_token"] = token
                page = self.aws.call("logs", "get-log-events", deadline=deadline, **params)
                for event in page.get("events", []):
                    remote_log("cloudwatch/" + name.rsplit("/", 1)[-1], event["message"])
                    self.events += 1
                new_token = page["nextForwardToken"]
                self.tokens[name] = new_token
                if new_token == token:
                    break
                token = new_token
            else:
                self.caught_up = False

    def drain(self, seconds, poll):
        if self.disabled or seconds == 0:
            return
        log(f"Waiting up to {seconds}s for delayed CloudWatch output")
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline and not self.disabled:
                self.poll(deadline)
                time.sleep(max(0, min(poll, deadline - time.monotonic())))
        except DeadlineExceeded:
            pass
        if not self.events:
            log("[warning] No CloudWatch events received; use the SSM backup below")
        if not self.caught_up:
            log("[warning] CloudWatch output is still catching up; remaining logs are in CloudWatch")


class Runner:
    def __init__(self, config, aws):
        self.config = config
        self.aws = aws
        self.command_id = None
        self.status = "Pending"
        self.exit_code = ""
        self.snapshot = {}
        self.seen = {"stdout": "", "stderr": ""}
        self.cloudwatch = None

    def output(self, **values):
        path = os.environ.get("GITHUB_OUTPUT")
        if path:
            with open(path, "a", encoding="utf-8") as file:
                for key, value in values.items():
                    file.write(f"{key}={value}\n")

    def ssm_output(self, final=False):
        # Retain the original SSM output path, even when CloudWatch is enabled.
        if self.cloudwatch and not self.cloudwatch.disabled and not final:
            return
        for stream, key in (("stdout", "StandardOutputContent"), ("stderr", "StandardErrorContent")):
            current = self.snapshot.get(key) or ""
            previous = self.seen[stream]
            delta = current[len(previous):] if current.startswith(previous) else current
            if delta:
                remote_log("ssm/" + stream, delta)
            self.seen[stream] = current
            limit = 24000 if stream == "stdout" else 8000
            if final and len(current) >= limit:
                log(f"[warning] SSM {stream} reached its {limit}-character limit; full logs require CloudWatch")

    def cancel(self):
        if self.command_id and self.status not in TERMINAL:
            log(f"Requesting cancellation of SSM command {self.command_id}")
            try:
                self.aws.call("ssm", "cancel-command", command_id=self.command_id,
                              instance_ids=self.config.instance_id,
                              deadline=time.monotonic() + 10)
            except (AwsError, DeadlineExceeded) as exc:
                log(f"[warning] Cancellation could not be confirmed: {exc}")

    def resolve_instance(self):
        if self.config.instance_id:
            return
        response = self.aws.call("ec2", "describe-instances", payload={"Filters": [
            {"Name": "tag:Name", "Values": [self.config.instance_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]})
        instances = [item["InstanceId"] for reservation in response.get("Reservations", [])
                     for item in reservation.get("Instances", [])]
        # DescribeInstances is paginated, including when a Name tag is supplied.
        while response.get("NextToken"):
            response = self.aws.call("ec2", "describe-instances", payload={"Filters": [
                {"Name": "tag:Name", "Values": [self.config.instance_name]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ], "NextToken": response["NextToken"]})
            instances.extend(item["InstanceId"] for reservation in response.get("Reservations", [])
                             for item in reservation.get("Instances", []))
        if len(instances) != 1:
            raise ValueError(f"Name tag matched {len(instances)} running instances; provide a unique name or instance-id")
        self.config.instance_id = instances[0]

    def submit(self):
        self.resolve_instance()
        for _, value in self.config.exports:
            if value and os.environ.get("GITHUB_ACTIONS") == "true":
                log("::add-mask::" + workflow_escape(value))
        payload = {
            "InstanceIds": [self.config.instance_id],
            "DocumentName": "AWS-RunShellScript",
            "TimeoutSeconds": max(30, self.config.timeout),
            "Parameters": {"commands": remote_commands(self.config),
                           "executionTimeout": [str(self.config.timeout)]},
        }
        if self.config.cloudwatch:
            payload["CloudWatchOutputConfig"] = {
                "CloudWatchOutputEnabled": True,
                "CloudWatchLogGroupName": self.config.group,
            }
        log(f"Sending SSM command to {self.config.instance_id}")
        # Do not resubmit on an ambiguous network failure, which could run the script twice.
        response = self.aws.call("ssm", "send-command", payload=payload)
        self.command_id = response["Command"]["CommandId"]
        self.output(**{"command-id": self.command_id})
        log(f"Command ID: {self.command_id}")
        if self.config.cloudwatch:
            self.cloudwatch = CloudWatch(self.aws, self.config.group, self.command_id, self.config.instance_id)
            log(f"CloudWatch log group: {self.config.group}")

    def monitor(self):
        deadline = time.monotonic() + self.config.timeout
        failures = 0
        last_status = None
        last_heartbeat = time.monotonic()
        while True:
            if time.monotonic() >= deadline:
                raise DeadlineExceeded(f"Timed out after {self.config.timeout}s")
            try:
                self.snapshot = self.aws.call("ssm", "get-command-invocation",
                                              command_id=self.command_id,
                                              instance_id=self.config.instance_id, deadline=deadline)
                failures = 0
                self.status = self.snapshot["Status"]
                self.exit_code = self.snapshot.get("ResponseCode", "")
                now = time.monotonic()
                if self.status != last_status or now - last_heartbeat >= 30:
                    elapsed = int(self.config.timeout - (deadline - now))
                    log(f"SSM status: {self.status} ({self.snapshot.get('StatusDetails', self.status)}); elapsed {elapsed}s")
                    last_status = self.status
                    last_heartbeat = now
                if self.status in TERMINAL:
                    break
                self.ssm_output()
                if self.status not in ACTIVE:
                    raise RuntimeError(f"Unexpected SSM status: {self.status}")
            except AwsError as exc:
                if exc.code != "InvocationDoesNotExist":
                    if exc.code not in TRANSIENT:
                        raise
                    failures += 1
                    log(f"[warning] SSM polling attempt {failures}/5: {exc}")
                    if failures >= 5:
                        raise
            if self.cloudwatch:
                self.cloudwatch.poll(deadline)
            time.sleep(max(0, min(self.config.poll, deadline - time.monotonic())))
        self.output(status=self.status, **{"exit-code": self.exit_code})
        if self.cloudwatch:
            self.cloudwatch.drain(self.config.drain, self.config.poll)
            log("SSM backup output (may repeat CloudWatch output; SSM character limits apply):")
        self.ssm_output(final=True)
        log(f"Final SSM status: {self.status}; exit code: {self.exit_code}")
        return 0 if self.status == "Success" and self.exit_code == 0 else 1

    def run(self):
        try:
            self.submit()
            return self.monitor()
        except (RuntimeError, ValueError, OSError) as exc:
            log(f"[error] {exc}")
            if not self.command_id and isinstance(exc, AwsError) and exc.code in TRANSIENT:
                log("[warning] Submission result may be unknown; check SSM before rerunning")
            self.cancel()
            if self.command_id:
                # Refresh once after cancellation, preserving the last known snapshot on failure.
                try:
                    self.snapshot = self.aws.call("ssm", "get-command-invocation",
                                                  command_id=self.command_id,
                                                  instance_id=self.config.instance_id,
                                                  deadline=time.monotonic() + 5)
                except (AwsError, DeadlineExceeded):
                    pass
                if self.cloudwatch:
                    try:
                        self.cloudwatch.poll(time.monotonic() + 5)
                    except DeadlineExceeded:
                        pass
                log("SSM backup output after monitoring stopped:")
                self.ssm_output(final=True)
                outcome = "TimedOut" if isinstance(exc, DeadlineExceeded) else "Cancelled" if isinstance(exc, Interrupted) else "MonitoringFailed"
                self.output(status=outcome, **{"exit-code": self.snapshot.get("ResponseCode", "")})
            return 128 + exc.signum if isinstance(exc, Interrupted) else 1


def main():
    try:
        config = Config.from_env(os.environ)
        if not shutil.which("aws"):
            raise ValueError("aws CLI not found on runner")
    except ValueError as exc:
        log(f"[error] {exc}")
        return 2

    def interrupted(signum, _frame):
        # Ignore subsequent cancellation signals while attempting remote cancellation.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise Interrupted(signum)

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    return Runner(config, AwsCli()).run()


if __name__ == "__main__":
    sys.exit(main())
