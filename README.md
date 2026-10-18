# aws-ec2-ssm-script-runner

Reusable GitHub Action that downloads a shell script from S3 and runs it on an
EC2 instance through AWS Systems Manager (SSM), without SSH.

**Release status:** `v0` is a prerelease for controlled stability testing.
The action needs real workflow use and validation before a stable `v1` release.
Examples use `v0`; pin a reviewed commit SHA when reproducibility is required.

## Usage

Configure AWS credentials and the region before invoking the action. OIDC is
recommended. Either `instance-id` or `instance-name` is required; the ID takes
precedence. A Name tag must match exactly one running instance.

```yaml
permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: eu-west-1

      - name: Run deployment script on EC2
        id: deploy
        uses: karaalv/aws-ec2-ssm-script-runner@v0
        with:
          instance-name: accounting-staging
          bucket-name: example-deployment-scripts
          script-location: infra/deploy/rollout.sh
          env-vars-for-script: |
            GIT_BRANCH=${{ github.ref_name }}
            DEPLOY_ENV=staging
```

## Logging

By default, the action polls SSM and prints stdout and stderr into the GitHub
Actions step log. Each response is read once for status, output and exit code.
Only newly available output is printed. SSM generally returns output after the
command finishes, limited to the first 24,000 stdout characters and 8,000 stderr
characters. This mode does not require CloudWatch permissions.

To see output while the script is running, enable CloudWatch logging:

```yaml
with:
  instance-id: i-0123456789abcdef0
  bucket-name: example-deployment-scripts
  script-location: infra/deploy/rollout.sh
  cloudwatch-logs: 'true'
  cloudwatch-log-group: /aws/ssm/deployments
  cloudwatch-drain-seconds: '35'
```

The data flow is:

```text
EC2 script stdout/stderr
  -> SSM Agent
  -> CloudWatch Logs
  -> action polls new events
  -> GitHub Actions step log
```

The action enables SSM's CloudWatch output, discovers streams using the command
ID and instance ID, and reads each stream from its saved forward token. Repeated
messages remain distinct events; previously printed events are not replayed.
Every output line is prefixed with its source, for example
`[cloudwatch/stdout]`, `[cloudwatch/stderr]`, `[ssm/stdout]` or `[ssm/stderr]`.
The prefixes also prevent remote output from being interpreted as GitHub workflow
commands. Separate stdout and stderr streams do not guarantee combined ordering.

AWS uploads output every 30 seconds or when its buffer exceeds 200 KB. Script
and application buffering can add further delay. After SSM reports completion,
the action waits an additional 35 seconds by default to collect delayed events.
This is a bounded collection window, not a guarantee that all CloudWatch events
have arrived. Set `cloudwatch-drain-seconds` to `0` to skip that final collection
window. Logs remain in CloudWatch according to the log group's retention policy.

**SSM output remains the backup.** When CloudWatch reading fails permanently,
the action warns and uses SSM output. Temporary CloudWatch errors are retried on
later polls. At completion, the action also prints the SSM output snapshot even
if CloudWatch worked, so the labelled backup may repeat the streamed content.
CloudWatch failures do not change the remote script's success or failure result.
The SSM backup still has AWS's character limits; warnings flag output at those
limits. A logging permission failure can therefore leave only the limited SSM
output visible in GitHub.

CloudWatch logging adds AWS log ingestion, storage and applicable API charges.
Pre-create a dedicated log group with an appropriate retention period. The action
does not change retention or configure IAM. See the
[AWS SSM logging documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-rc-setting-up-cwlogs.html)
and [GetCommandInvocation limits](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetCommandInvocation.html).

## Requirements and IAM permissions

The composite action sets up Python 3.14 before running. Consumers do not
need a separate Python setup step. The runner requires Bash and AWS CLI v2. The EC2
instance requires SSM Agent, AWS CLI, a POSIX shell, `mktemp`, and the interpreter
named in the downloaded script's shebang. The script runs as the SSM Agent's
execution user, normally root, rather than your SSH user.

The instance needs network access to SSM and S3, plus CloudWatch Logs when enabled.

### GitHub workflow role

- `ssm:SendCommand`, scoped to the target instance and `AWS-RunShellScript` document.
- `ssm:GetCommandInvocation` for status and backup output.
- `ssm:CancelCommand` for best-effort cancellation on timeout, interruption or
  an unrecoverable monitoring failure.
- `ec2:DescribeInstances` only when resolving `instance-name`.
- With CloudWatch enabled: `logs:DescribeLogStreams` and `logs:GetLogEvents`,
  scoped to the configured log group and its streams.

### EC2 instance role

- `AmazonSSMManagedInstanceCore` or equivalent SSM permissions.
- `s3:GetObject` for the script, plus applicable KMS decrypt permissions for
  encrypted objects.
- With CloudWatch enabled: `logs:CreateLogStream`, `logs:DescribeLogStreams` and
  `logs:PutLogEvents` for the configured group, plus the group discovery permissions
  described in the AWS documentation. If SSM must create the group, also grant
  `logs:CreateLogGroup`. AWS documents `CloudWatchAgentServerPolicy` as a managed
  alternative; a custom policy can restrict access to the chosen group.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `instance-id` | No | Empty | Target EC2 instance ID; takes precedence over Name. |
| `instance-name` | No | Empty | Name tag matching exactly one running instance. |
| `bucket-name` | Yes | None | S3 bucket containing the script. |
| `script-location` | Yes | None | S3 object key of the script. |
| `env-vars-for-script` | No | Empty | Newline-separated `KEY=VALUE` assignments. |
| `script-name` | No | Object basename | Local filename, without directory components. |
| `poll-interval-seconds` | No | `5` | Positive integer between monitoring polls. |
| `timeout-seconds` | No | `1800` | Remote execution timeout and local monitoring limit, from 1 to 172800 seconds. |
| `cloudwatch-logs` | No | `false` | Enable SSM CloudWatch output and relay events into the workflow log. |
| `cloudwatch-log-group` | No | `/aws/ssm/AWS-RunShellScript` | Log group receiving command output. |
| `cloudwatch-drain-seconds` | No | `35` | Extra collection window after completion, from 0 to 300 seconds. |

## Outputs and failure handling

| Output | Description |
|---|---|
| `command-id` | Written immediately after SSM accepts the command, before polling. |
| `status` | Final SSM status, or local `MonitoringFailed`, `TimedOut` or `Cancelled` when monitoring stops early. |
| `exit-code` | Last available remote response code; may be `-1` or empty if execution has not completed. |

The action succeeds only when SSM reports `Success` and remote exit code `0`.
SSM propagation delays and transient polling failures are retried. Persistent
transient failures are bounded to five consecutive failed polls. Authentication,
permission and invalid-request errors fail visibly. A successful AWS request
resets the failure counter. Individual CLI calls have bounded timeouts.

The local monitoring timeout includes time waiting for SSM delivery and execution.
SSM also receives an execution timeout and a delivery timeout, with AWS's minimum
30-second delivery timeout. The optional CloudWatch collection window is additional
to monitoring time. Cancellation and final diagnostics can add up to 20 seconds.
Set the enclosing GitHub job timeout to allow for these operations.

On SIGINT or SIGTERM, the action attempts to cancel the remote command and collect
one final SSM response. Cancellation is best effort, not proof that EC2 execution
has stopped. A runner killed before the handler can finish cannot cancel remotely;
the SSM execution timeout remains the backstop. An ambiguous submission failure
is not automatically retried because that could execute the script twice.

Each execution uses its own temporary directory. An exit trap removes that
execution's files on success or failure while preserving the script's exit code.
Shell arguments and environment values are quoted, and unconditional command
tracing is disabled.

## Environment variables

Values use newline-separated `KEY=VALUE` notation. Blank lines and comment lines
are ignored. Values retain spaces and additional equals signs. Multiline values
are not supported. Names beginning with `SSM_RUNNER_` are reserved.

```yaml
env-vars-for-script: |
  APP_ENV=production
  LOG_LEVEL=info
  DEPLOY_VERSION=2026-10-18
```

Values are registered for masking in GitHub Actions and are not put in AWS CLI
process arguments. Masking does not redact CloudWatch or SSM records. SSM stores
command parameters, so obtain sensitive runtime credentials on the instance from
Secrets Manager or Parameter Store rather than embedding them here. The action
cannot prevent a downloaded script from printing secrets or enabling its own
command tracing.

## Development

The composite action sets up Python, then calls `scripts/run.sh`. The shell
entry point runs `python -u -m ec2_ssm_runner` from the action checkout. The
package uses only the standard library; there are no runtime pip dependencies.
Python setup changes the Python available to subsequent steps in the calling job.

```text
ec2_ssm_runner/
  __main__.py         Package entry point
  cli.py              Environment checks and signal handlers
  config.py           Action input parsing and validation
  runner.py           Submission, polling and failure cleanup
  remote.py           Quoted shell commands executed on EC2
  github.py           Action outputs and value masking
  errors.py           Execution errors and retryable AWS codes
  aws/
    types.py          AWS response shapes and transport interface
    cli.py            Bounded AWS CLI requests and JSON handling
    ssm.py            Instance lookup and SSM API operations
  logs/
    console.py        Prefixed console output
    cloudwatch.py     Stream discovery, cursors and final collection
    ssm.py            Direct SSM output backup and limits
tests/
  fixtures/           Fake AWS executable for process tests
  support.py          Shared responses, input fixtures and fake clock
  test_*.py           Tests grouped by module
```

Start with `runner.py` for the execution flow. AWS operations live under `aws/`,
and both output paths live under `logs/`. `config.py` owns input validation;
`remote.py` owns the shell payload sent to the instance. Functions declare
parameter and return types, with `TypedDict` response shapes and an
`AwsTransport` protocol describing the real client and test double interface.
The AWS adapters narrow JSON responses to those shapes using `cast`; the
annotations do not add runtime validation or dependencies.

The Ruff configuration in `pyproject.toml` matches the portfolio agent exactly:
70-character lines, single quotes, tabs, automatic fixes, and the `E`, `F`, `I`,
`B` and `UP` rules. Ruff is a development dependency pinned in
`requirements-dev.txt`; consumers do not install it. Pyright is also pinned
there and checks the runner package in CI.

```sh
uv venv --python 3.14
uv pip sync --python .venv/bin/python requirements-dev.txt
```

```sh
.venv/bin/ruff check --no-fix .
.venv/bin/ruff format --check .
.venv/bin/pyright ec2_ssm_runner
bash -n scripts/run.sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

Tests mock AWS calls and execute generated remote commands against a local fake
AWS CLI. They cover pagination, delayed events, fallback, permissions, exit codes,
timeouts, cancellation, quoting and cleanup. They do not prove live AWS delivery
or IAM configuration. GitHub CI runs Ruff, Pyright and the regression tests, plus a
composite-action smoke test against the fake AWS executable. The smoke test
exercises Python setup, action input wiring and published step outputs without
contacting AWS.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
