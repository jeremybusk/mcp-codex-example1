import os
from pathlib import Path

import pytest
import yaml

import server


def configure(tmp_path: Path, commands: dict) -> None:
    path = tmp_path / "commands.yaml"
    path.write_text(yaml.safe_dump({"commands": commands}), encoding="utf-8")
    server.CONFIG_PATH = path


def test_builds_only_typed_arguments(tmp_path):
    configure(tmp_path, {"logs": {
        "enabled": True,
        "argv": ["/bin/tool", "--lines", "{lines}", "{service}"],
        "parameters": {
            "lines": {"type": "integer", "min": 1, "max": 10},
            "service": {"type": "enum", "values": ["nginx"]},
        },
    }})
    argv, timeout = server.build_argv("logs", {"lines": 5, "service": "nginx"})
    assert argv == ["/bin/tool", "--lines", "5", "nginx"]
    assert timeout == 20


@pytest.mark.parametrize("parameters", [
    {"lines": "5; id", "service": "nginx"},
    {"lines": 5, "service": "nginx; id"},
    {"lines": 5, "service": "nginx", "extra": "x"},
])
def test_rejects_unapproved_input(tmp_path, parameters):
    configure(tmp_path, {"logs": {
        "enabled": True,
        "argv": ["/bin/tool", "{lines}", "{service}"],
        "parameters": {
            "lines": {"type": "integer", "min": 1, "max": 10},
            "service": {"type": "enum", "values": ["nginx"]},
        },
    }})
    with pytest.raises((ValueError, TypeError)):
        server.build_argv("logs", parameters)


def test_rejects_disabled_command(tmp_path):
    configure(tmp_path, {"danger": {"enabled": False, "argv": ["/bin/true"]}})
    with pytest.raises(ValueError, match="not enabled"):
        server.build_argv("danger", {})
