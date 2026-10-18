"""Console output for local messages and remote log lines."""


def log(message):
	print(message, flush=True)


def remote_log(source, message):
	# Prefix lines to prevent remote GitHub workflow commands.
	for line in message.splitlines():
		log(f'[{source}] {line}')
