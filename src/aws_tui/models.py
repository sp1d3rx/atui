from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class InstanceSummary:
    instance_id: str
    name: str
    state: str
    instance_type: str
    private_ip: str | None
    public_ip: str | None
    availability_zone: str | None

    @property
    def display_name(self) -> str:
        return self.name or self.instance_id
