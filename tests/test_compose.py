"""Compose is configured from one settings file; check what it hands the container."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose is not available")


def service(*extra_files, **variables):
    """The resolved service, with settings from this environment only, never a local settings file."""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/nonexistent"),
           "MEDIA_ROOT": "/srv/media", "YOUTARR_INFO_DIR": "/srv/info", **variables}
    command = ["docker", "compose", "--env-file", os.devnull, "-f", "docker-compose.yml"]
    for name in extra_files:
        command += ["-f", name]
    done = subprocess.run([*command, "config", "--format", "json"], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["services"]["youtarr-routarr"]


@needs_compose
def test_blank_folder_turns_a_library_off_and_spaces_survive():
    env = service(SHOWS_FOLDER="YouTube Shows", MOVIES_FOLDER="")["environment"]
    assert env["SHOWS_DIR"] == "/media/YouTube Shows"
    assert env["MOVIES_DIR"] == "" and env["MUSIC_DIR"] == ""
    assert env["QUEUE_DIR"] == "/media/youtarr-queue" and env["DRY_RUN"] == "1"


@needs_compose
def test_defaults_and_published_image():
    config = service()
    assert config["image"] == "ghcr.io/jesp1999/youtarr-routarr:latest" and "build" not in config
    assert config["user"] == "1000:1000" and config["environment"]["TZ"] == "Etc/UTC"
    assert service("docker-compose.build.yml")["image"] == "youtarr-routarr:local"


def test_sample_lists_exactly_the_variables_compose_reads():
    used = set(re.findall(r"\$\{([A-Z_]+)", (ROOT / "docker-compose.yml").read_text()))
    listed = set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert used == listed
