from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import emet_sdk
from emet_sdk._delivery import captured, new_result


def test_every_result_has_the_same_keys() -> None:
    # emet-cli reads these; a change here is a schema_version bump
    keys = {
        "schema_version",
        "written_at",
        "status",
        "reason",
        "code",
        "trace_id",
        "trace",
        "conversation",
        "accepted",
        "batches_sent",
        "batches_not_sent",
        "unattributed",
        "late",
        "late_after_retry",
        "gaps",
    }
    assert set(new_result("sent")) == keys
    assert set(new_result("skipped", "no_spans")) == keys
    assert new_result("sent")["schema_version"] == 1


def test_captured_means_whole_attributed_and_without_gaps() -> None:
    assert captured(new_result("sent"))
    assert not captured(new_result("stopped", "timeout"))
    assert not captured({**new_result("sent"), "unattributed": True})
    assert not captured({**new_result("sent"), "late_after_retry": True})
    gaps = {**new_result("sent")["gaps"], "missing_tool_results": ["get_spend"]}
    assert not captured({**new_result("sent"), "gaps": gaps})


def test_the_delivery_file_is_replaced_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".emet").mkdir()
    write = emet_sdk.write_delivery_file(".emet/last-delivery.json")
    monkeypatch.chdir("/")
    write(new_result("sent", trace_id="first"))
    write(new_result("skipped", "no_spans"))
    target = tmp_path / ".emet" / "last-delivery.json"
    assert json.loads(target.read_text())["reason"] == "no_spans"
    assert os.listdir(tmp_path / ".emet") == ["last-delivery.json"]


def test_a_missing_folder_is_not_created_or_raised(tmp_path: Path) -> None:
    write = emet_sdk.write_delivery_file(tmp_path / "missing" / "last-delivery.json")
    write(new_result("sent"))
    assert not (tmp_path / "missing").exists()


def test_a_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    emet_sdk.write_delivery_file(tmp_path / "last-delivery.json")(new_result("sent"))
    assert os.listdir(tmp_path) == []
