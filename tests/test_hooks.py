"""Tests for PostToolUse progress hook (slack-progress.sh)."""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

HOOK_SCRIPT = Path.home() / ".snowflake/cortex/hooks/slack-progress.sh"


def _has_jq():
    return subprocess.run(["which", "jq"], capture_output=True).returncode == 0


@pytest.fixture
def mock_notify(tmp_path):
    """Create a mock coco-notify that records calls."""
    calls_file = tmp_path / "notify_calls.txt"
    mock = tmp_path / "coco-notify"
    mock.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls_file}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return mock, calls_file


def _run_hook(payload: dict, tmp_path: Path, mock_notify_dir: Path, flag_dir: Path | None = None):
    """Run the hook script with a JSON payload on stdin."""
    env = {
        **os.environ,
        "PATH": f"{mock_notify_dir}:{os.environ.get('PATH', '')}",
    }
    if flag_dir:
        env["COCO_PROGRESS_FLAG_DIR"] = str(flag_dir)

    return subprocess.run(
        [str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


@pytest.mark.skipif(not _has_jq(), reason="jq not installed")
def test_progress_hook_fires_for_bash(tmp_path):
    mock, calls_file = mock_notify(tmp_path) if False else (None, None)
    # Use fixture directly
    calls_file = tmp_path / "notify_calls.txt"
    mock = tmp_path / "coco-notify"
    mock.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls_file}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()

    result = _run_hook(
        {"tool_name": "Bash", "session_id": "test-progress", "hook_event_name": "PostToolUse"},
        tmp_path, tmp_path, flag_dir,
    )
    assert result.returncode == 0
    assert calls_file.exists(), f"coco-notify was not called. stderr: {result.stderr}"
    content = calls_file.read_text()
    assert "--type" in content
    assert "status" in content


@pytest.mark.skipif(not _has_jq(), reason="jq not installed")
def test_progress_hook_skips_insignificant_tool(tmp_path):
    calls_file = tmp_path / "notify_calls.txt"
    mock = tmp_path / "coco-notify"
    mock.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls_file}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()

    result = _run_hook(
        {"tool_name": "Read", "session_id": "test-skip", "hook_event_name": "PostToolUse"},
        tmp_path, tmp_path, flag_dir,
    )
    assert result.returncode == 0
    assert not calls_file.exists(), "coco-notify should NOT be called for Read tool"


@pytest.mark.skipif(not _has_jq(), reason="jq not installed")
def test_progress_hook_respects_cooldown(tmp_path):
    calls_file = tmp_path / "notify_calls.txt"
    mock = tmp_path / "coco-notify"
    mock.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls_file}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()

    # Create a fresh flag file (simulates recent notification)
    flag_file = flag_dir / "coco_progress_cooldown-sess"
    flag_file.touch()

    result = _run_hook(
        {"tool_name": "Bash", "session_id": "cooldown-sess", "hook_event_name": "PostToolUse"},
        tmp_path, tmp_path, flag_dir,
    )
    assert result.returncode == 0
    assert not calls_file.exists(), "coco-notify should be suppressed during cooldown"


@pytest.mark.skipif(not _has_jq(), reason="jq not installed")
def test_progress_hook_fires_after_cooldown_expires(tmp_path):
    calls_file = tmp_path / "notify_calls.txt"
    mock = tmp_path / "coco-notify"
    mock.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{calls_file}"\n')
    mock.chmod(mock.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    flag_dir = tmp_path / "flags"
    flag_dir.mkdir()

    # Create a stale flag file (4 minutes old — beyond 3-min cooldown)
    flag_file = flag_dir / "coco_progress_expired-sess"
    flag_file.touch()
    stale_time = time.time() - 241  # 4 min + 1 sec ago
    os.utime(flag_file, (stale_time, stale_time))

    result = _run_hook(
        {"tool_name": "Edit", "session_id": "expired-sess", "hook_event_name": "PostToolUse"},
        tmp_path, tmp_path, flag_dir,
    )
    assert result.returncode == 0
    assert calls_file.exists(), "coco-notify should fire after cooldown expires"
