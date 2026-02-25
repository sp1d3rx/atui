from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import boto3

from .models import InstanceSummary

DEFAULT_PROFILE = "default"
DEFAULT_REGION = "us-west-1"


def is_aws_cli_available() -> bool:
    return shutil.which("aws") is not None


@dataclass(slots=True, frozen=True)
class AwsInstance:
    instance_id: str
    profile: str = DEFAULT_PROFILE
    region: str = DEFAULT_REGION

    def build_ssm_shell_command(self) -> list[str]:
        return self._base_start_session_command()

    def build_port_forward_command(self, remote_port: int, local_port: int) -> list[str]:
        parameters = json.dumps(
            {
                "portNumber": [str(remote_port)],
                "localPortNumber": [str(local_port)],
            }
        )
        return [
            *self._base_start_session_command(),
            "--document-name",
            "AWS-StartPortForwardingSession",
            "--parameters",
            parameters,
        ]

    def _base_start_session_command(self) -> list[str]:
        return [
            "aws",
            "ssm",
            "start-session",
            "--target",
            self.instance_id,
            "--profile",
            self.profile or DEFAULT_PROFILE,
            "--region",
            self.region or DEFAULT_REGION,
        ]


class AwsEc2Service:
    def __init__(self, profile: str = DEFAULT_PROFILE, region: str = DEFAULT_REGION) -> None:
        self.profile = profile or DEFAULT_PROFILE
        self.region = region or DEFAULT_REGION
        self._session = boto3.Session(profile_name=self.profile, region_name=self.region)

    def list_instances(self) -> list[InstanceSummary]:
        ec2 = self._session.client("ec2")
        paginator = ec2.get_paginator("describe_instances")
        filters = [
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            }
        ]

        summaries: list[InstanceSummary] = []
        for page in paginator.paginate(Filters=filters):
            reservations = page.get("Reservations", [])
            for reservation in reservations:
                for instance in reservation.get("Instances", []):
                    summaries.append(self._to_summary(instance))

        summaries.sort(key=lambda item: (item.state != "running", item.display_name.lower()))
        return summaries

    @staticmethod
    def _to_summary(instance: dict[str, Any]) -> InstanceSummary:
        return InstanceSummary(
            instance_id=instance["InstanceId"],
            name=_tag_value(instance.get("Tags", []), "Name"),
            state=instance.get("State", {}).get("Name", "unknown"),
            instance_type=instance.get("InstanceType", "unknown"),
            private_ip=instance.get("PrivateIpAddress"),
            public_ip=instance.get("PublicIpAddress"),
            availability_zone=instance.get("Placement", {}).get("AvailabilityZone"),
        )


def build_ssm_shell_command(
    instance_id: str,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
) -> list[str]:
    return AwsInstance(instance_id=instance_id, profile=profile, region=region).build_ssm_shell_command()


def build_port_forward_command(
    instance_id: str,
    remote_port: int,
    local_port: int,
    profile: str = DEFAULT_PROFILE,
    region: str = DEFAULT_REGION,
) -> list[str]:
    return AwsInstance(instance_id=instance_id, profile=profile, region=region).build_port_forward_command(
        remote_port=remote_port,
        local_port=local_port,
    )


def build_mock_instances(region: str = DEFAULT_REGION) -> list[InstanceSummary]:
    short_region = (region or DEFAULT_REGION).replace("-", "")
    return [
        InstanceSummary(
            instance_id=f"i-{short_region}a1b2c3d4e5f6",
            name="demo-bastion",
            state="running",
            instance_type="t3.micro",
            private_ip="10.0.1.21",
            public_ip="54.10.10.21",
            availability_zone=f"{region}a",
        ),
        InstanceSummary(
            instance_id=f"i-{short_region}112233445566",
            name="demo-app-01",
            state="running",
            instance_type="t3.small",
            private_ip="10.0.2.34",
            public_ip=None,
            availability_zone=f"{region}b",
        ),
        InstanceSummary(
            instance_id=f"i-{short_region}998877665544",
            name="demo-rabbitmq",
            state="stopped",
            instance_type="t3.medium",
            private_ip="10.0.3.10",
            public_ip=None,
            availability_zone=f"{region}c",
        ),
    ]


def _tag_value(tags: Iterable[dict[str, str]], key: str) -> str:
    for tag in tags:
        if tag.get("Key") == key:
            return tag.get("Value", "")
    return ""
