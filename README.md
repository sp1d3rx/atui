# AWS TUI (Textual + Python 3.12)

A lightweight terminal UI that:

- Lists EC2 instances.
- Connects to a selected instance console via AWS Systems Manager Session Manager (`ssm start-session`).
- Starts SSM port forwarding from a selected instance.

Defaults:

- AWS profile: `default`
- AWS region: `us-west-1`

## Requirements

- Python 3.12+
- AWS CLI v2
- Session Manager plugin installed for AWS CLI
- IAM permissions for:
  - `ec2:DescribeInstances`
  - `ssm:StartSession`
  - `ssm:DescribeSessions` (optional but commonly needed)
  - `ssm:TerminateSession` (recommended)

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Run

```bash
./aws-tui
```

Or override defaults:

```bash
./aws-tui --profile my-profile --region us-east-1 --ports-config ./port-forwards.yaml
```

## Controls

- `r`: refresh EC2 list
- `c`: connect to selected instance via SSM console session
- `p`: add a new named port forward for selected instance
- `Enter`: open selected instance details (active forwards + history)
- `y`: copy current command to clipboard
- `q`: quit

Buttons are also available in the top bar.

Bottom area:

- Command bar shows the exact AWS CLI command for the selected instance in a copyable input field (`Copy` button available).
- Activity log shows recent actions/events (6 lines visible).

Instance details:

- `Add forward`: add a new named port forward.
- `Start selected`: start a selected stopped/history port forward.
- `Stop selected`: stop a selected active port forward.

Quit behavior:

- If active port forwards exist, quit shows a list (forward name + machine) for confirmation, then gracefully stops forwards before exiting.

If `aws` CLI is not installed, the app runs in simulated mode:

- EC2 list is populated with sample instances.
- Connect/port-forward actions are simulated but still show the exact command in the preview line.

## Port Forward History (SQLite)

Port-forward history is persisted in SQLite.

- Default file: `./port-history.db` (project root)
- Override path: `./aws-tui --history-file /path/to/port-history.db`

## Port Forward Presets (YAML)

Port-forward defaults and preset ports are loaded from `port-forwards.yaml` in the current working directory.

You can point to a different file:

```bash
./aws-tui --ports-config /path/to/port-forwards.yaml
```
