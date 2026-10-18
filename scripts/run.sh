#!/usr/bin/env bash
set -euo pipefail

need() {
	if [ -z "${!1:-}" ]; then
		echo "Missing required env: $1" >&2
		exit 2
	fi
}

need_one_of() {
	local a="${!1:-}"
	local b="${!2:-}"
	if [ -z "$a" ] && [ -z "$b" ]; then
		echo "Missing one of: $1 or $2" >&2
		exit 2
	fi
}

fail_fast_region() {
	if [ -z "${AWS_REGION:-}" ] && \
		[ -z "${AWS_DEFAULT_REGION:-}" ]
	then
		echo "AWS region not set." >&2
		echo "Set aws-region in" \
			"configure-aws-credentials." >&2
		exit 2
	fi
}

need "INPUT_BUCKET_NAME"
need "INPUT_SCRIPT_LOCATION"
need_one_of "INPUT_INSTANCE_ID" "INPUT_INSTANCE_NAME"

BUCKET_NAME="$INPUT_BUCKET_NAME"
SCRIPT_LOCATION="$INPUT_SCRIPT_LOCATION"
ENV_VARS="${INPUT_ENV_VARS:-}"
SCRIPT_NAME="${INPUT_SCRIPT_NAME:-}"
POLL_INTERVAL="${INPUT_POLL_INTERVAL:-5}"
TIMEOUT="${INPUT_TIMEOUT:-1800}"

INSTANCE_ID="${INPUT_INSTANCE_ID:-}"
INSTANCE_NAME="${INPUT_INSTANCE_NAME:-}"

if [ -z "$SCRIPT_NAME" ]; then
	SCRIPT_NAME="$(basename "$SCRIPT_LOCATION")"
fi

if ! command -v aws >/dev/null 2>&1; then
	echo "aws cli not found on runner" >&2
	exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
	echo "python3 not found on runner" >&2
	exit 2
fi

fail_fast_region

resolve_instance_id() {
	local name="$1"

	local ids
	ids="$(
		aws ec2 describe-instances \
			--filters \
				"Name=tag:Name,Values=$name" \
				"Name=instance-state-name,Values=running" \
			--query \
				"Reservations[].Instances[].InstanceId" \
			--output text
	)"

	if [ -z "$ids" ]; then
		echo "No running instance for Name=$name" >&2
		exit 1
	fi

	local count
	count="$(echo "$ids" | wc -w | tr -d ' ')"

	if [ "$count" -ne 1 ]; then
		echo "Name=$name matched $count instances" >&2
		echo "Matches: $ids" >&2
		echo "Make Name unique or pass instance-id" >&2
		exit 1
	fi

	echo "$ids"
}

if [ -z "$INSTANCE_ID" ]; then
	INSTANCE_ID="$(resolve_instance_id "$INSTANCE_NAME")"
fi

START_TS="$(date +%s)"

echo "Instance: $INSTANCE_ID"
echo "Bucket:   $BUCKET_NAME"
echo "Key:      $SCRIPT_LOCATION"
echo "Name:     $SCRIPT_NAME"

# Build export commands from ENV_VARS.
# Format: newline-separated KEY=VALUE.
EXPORT_CMDS=""
if [ -n "$ENV_VARS" ]; then
	while IFS= read -r line; do
		[ -z "$line" ] && continue
		case "$line" in
			\#*) continue ;;
		esac
		if ! echo "$line" | grep -q '='; then
			echo "Bad env var line: $line" >&2
			exit 2
		fi

		key="${line%%=*}"
		val="${line#*=}"

		if ! echo "$key" | \
			grep -Eq '^[A-Za-z_][A-Za-z0-9_]*$'
		then
			echo "Bad env var key: $key" >&2
			exit 2
		fi

		val_esc="${val//\'/\'\"\'\"\'}"
		EXPORT_CMDS="${EXPORT_CMDS}"$'\n'"export"
		EXPORT_CMDS="${EXPORT_CMDS} $key='$val_esc'"
	done <<< "$ENV_VARS"
fi

export BUCKET_NAME SCRIPT_LOCATION SCRIPT_NAME
export EXPORT_CMDS

COMMANDS_JSON="$(
python3 - <<'PY'
import json
import os

bucket = os.environ["BUCKET_NAME"]
key = os.environ["SCRIPT_LOCATION"]
name = os.environ["SCRIPT_NAME"]
exports = os.environ.get("EXPORT_CMDS", "")

cmds = [
	"set -eux",
	"echo '1. Creating deployment-scripts folder'",
	"mkdir -p ~/deployment-scripts",
	"echo '2. Pulling script from S3'",
	f"aws s3 cp s3://{bucket}/{key} "
	f"~/deployment-scripts/{name}",
	"echo '3. Making script executable'",
	f"chmod +x ~/deployment-scripts/{name}",
	"echo '4. Running script'",
]

if exports.strip():
	for line in exports.splitlines():
		line = line.strip()
		if line:
			cmds.append(line)

cmds += [
	f"~/deployment-scripts/{name}",
	"echo '5. Cleaning up'",
	"rm -rf ~/deployment-scripts",
]

print(json.dumps(cmds))
PY
)"

echo "Sending SSM command..."
COMMAND_ID="$(
aws ssm send-command \
	--instance-ids "$INSTANCE_ID" \
	--document-name "AWS-RunShellScript" \
	--parameters "commands=$COMMANDS_JSON" \
	--query "Command.CommandId" \
	--output text
)"

echo "Command ID: $COMMAND_ID"

status="InProgress"
exit_code=""

while :; do
	now="$(date +%s)"
	elapsed="$((now - START_TS))"

	if [ "$elapsed" -ge "$TIMEOUT" ]; then
		echo "Timed out after ${TIMEOUT}s" >&2
		status="TimedOut"
		break
	fi

	status="$(
	aws ssm get-command-invocation \
		--instance-id "$INSTANCE_ID" \
		--command-id "$COMMAND_ID" \
		--query "Status" \
		--output text 2>/dev/null || echo "Pending"
	)"

	echo "Status: $status"

	echo "[stdout]"
	aws ssm get-command-invocation \
		--instance-id "$INSTANCE_ID" \
		--command-id "$COMMAND_ID" \
		--query "StandardOutputContent" \
		--output text 2>/dev/null | \
		sed 's/^/[stdout] /' || true

	echo "[stderr]"
	aws ssm get-command-invocation \
		--instance-id "$INSTANCE_ID" \
		--command-id "$COMMAND_ID" \
		--query "StandardErrorContent" \
		--output text 2>/dev/null | \
		sed 's/^/[stderr] /' || true

	case "$status" in
		Pending|InProgress|Delayed)
			sleep "$POLL_INTERVAL"
			;;
		Success|Cancelled|TimedOut|Failed|Cancelling)
			break
			;;
		*)
			echo "Unknown status: $status" >&2
			sleep "$POLL_INTERVAL"
			;;
	esac
done

exit_code="$(
aws ssm get-command-invocation \
	--instance-id "$INSTANCE_ID" \
	--command-id "$COMMAND_ID" \
	--query "ResponseCode" \
	--output text 2>/dev/null || echo ""
)"

echo "Exit code: $exit_code"

{
	echo "command-id=$COMMAND_ID"
	echo "status=$status"
	echo "exit-code=$exit_code"
} >> "$GITHUB_OUTPUT"

if [ "$status" != "Success" ]; then
	echo "❌ SSM command did not succeed: $status" >&2
	exit 1
fi

if [ -n "$exit_code" ] && [ "$exit_code" -ne 0 ]; then
	echo "❌ Script failed with exit code $exit_code" >&2
	exit 1
fi

echo "✅ Deployment succeeded"
