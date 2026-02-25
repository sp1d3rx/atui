from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True, frozen=True)
class PortPreset:
    key: str
    label: str
    remote_port: int
    local_port: int


@dataclass(slots=True, frozen=True)
class PortForwardConfig:
    default_remote_port: int
    default_local_port: int
    presets: tuple[PortPreset, ...]


DEFAULT_PORT_FORWARD_CONFIG = PortForwardConfig(
    default_remote_port=22,
    default_local_port=2222,
    presets=(
        PortPreset("ssh", "SSH (22)", 22, 2222),
        PortPreset("http", "HTTP (80)", 80, 8080),
        PortPreset("https", "HTTPS (443)", 443, 8443),
        PortPreset("postgres", "PostgreSQL (5432)", 5432, 5432),
        PortPreset("mysql", "MySQL (3306)", 3306, 3306),
        PortPreset("redis", "Redis (6379)", 6379, 6379),
        PortPreset("mongodb", "MongoDB (27017)", 27017, 27017),
        PortPreset("rdp", "RDP (3389)", 3389, 3389),
        PortPreset("rabbitmq-amqp", "RabbitMQ AMQP (5672)", 5672, 5672),
        PortPreset("rabbitmq-amqps", "RabbitMQ AMQP SSL (5671)", 5671, 5671),
        PortPreset("rabbitmq-admin", "RabbitMQ Admin (15672)", 15672, 15672),
        PortPreset("rabbitmq-admin-ssl", "RabbitMQ Admin SSL (15671)", 15671, 15671),
    ),
)


def load_port_forward_config(config_path: str | Path | None = None) -> PortForwardConfig:
    path = Path(config_path).expanduser() if config_path else Path("port-forwards.yaml")
    if not path.is_file():
        return DEFAULT_PORT_FORWARD_CONFIG

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}

    default_remote_port = _coerce_port(
        _safe_mapping_get(loaded, "default_remote_port"),
        fallback=DEFAULT_PORT_FORWARD_CONFIG.default_remote_port,
    )
    default_local_port = _coerce_port(
        _safe_mapping_get(loaded, "default_local_port"),
        fallback=DEFAULT_PORT_FORWARD_CONFIG.default_local_port,
    )
    presets = _parse_presets(_safe_mapping_get(loaded, "presets"))
    return PortForwardConfig(
        default_remote_port=default_remote_port,
        default_local_port=default_local_port,
        presets=presets if presets else DEFAULT_PORT_FORWARD_CONFIG.presets,
    )


def _parse_presets(value: Any) -> tuple[PortPreset, ...]:
    parsed: list[PortPreset] = []
    try:
        iterator = iter(value)
    except TypeError:
        return ()

    for item in iterator:
        try:
            key = str(item["key"]).strip()
        except (KeyError, TypeError):
            continue

        label = str(_safe_mapping_get(item, "label", key)).strip() or key
        remote_port = _coerce_port(_safe_mapping_get(item, "remote_port"), fallback=None)
        local_port = _coerce_port(_safe_mapping_get(item, "local_port"), fallback=remote_port)
        if not key or remote_port is None or local_port is None:
            continue
        parsed.append(
            PortPreset(
                key=key,
                label=label,
                remote_port=remote_port,
                local_port=local_port,
            )
        )
    return tuple(parsed)


def _coerce_port(value: Any, fallback: int | None) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback

    if 1 <= port <= 65535:
        return port
    return fallback


def _safe_mapping_get(mapping: Any, key: str, fallback: Any = None) -> Any:
    try:
        return mapping[key]
    except (KeyError, TypeError):
        return fallback
