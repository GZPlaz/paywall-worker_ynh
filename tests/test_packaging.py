"""Checks of the YunoHost packaging that YunoHost itself would only report
on a real server."""

import os
import subprocess
import sys
import tomllib

ROOT = os.path.join(os.path.dirname(__file__), "..")
READONLY_TYPES = {"alert", "markdown", "display_text", "button"}
# Settings YunoHost stores itself, or that the install script generates
SET_BY_YUNOHOST = {"domain", "path", "port", "install_dir", "data_dir"}
SET_BY_INSTALL_SCRIPT = {"signing_secret", "flask_secret", "origin_secret"}


def load(name):
    with open(os.path.join(ROOT, name), "rb") as fh:
        return tomllib.load(fh)


def defaults_written_at_install():
    """Run the CLI exactly like scripts/_common.sh does at install time."""
    out = subprocess.run(
        [sys.executable, "-m", "paywall", "config-defaults"],
        env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src")), capture_output=True, check=True,
    ).stdout.decode()
    parts = out.split("\0")[:-1]
    return dict(zip(parts[::2], parts[1::2]))


def config_panel_options():
    panel = load("config_panel.toml")
    for panel_id, panel_body in panel.items():
        if not isinstance(panel_body, dict):
            continue
        for section in panel_body.values():
            if not isinstance(section, dict):
                continue
            for option_id, option in section.items():
                if isinstance(option, dict) and "type" in option:
                    yield option_id, option


def test_every_config_panel_setting_exists_after_install():
    """YunoHost refuses to display the config panel ('Config panel question
    X should be initialized with a value during install or upgrade') when a
    question has no stored setting and no default."""
    install_questions = set(load("manifest.toml")["install"])
    stored = set(defaults_written_at_install()) | install_questions | SET_BY_YUNOHOST | SET_BY_INSTALL_SCRIPT
    missing = [
        option_id for option_id, option in config_panel_options()
        if option["type"] not in READONLY_TYPES and option.get("bind") != "null"
        and option_id not in stored and "default" not in option
    ]
    assert missing == []


def test_password_questions_have_no_default():
    # YunoHost rejects a default on password questions, so they must be
    # initialized by the install script instead.
    for option_id, option in config_panel_options():
        if option["type"] == "password":
            assert "default" not in option, option_id
