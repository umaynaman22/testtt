import importlib.util

import pytest

from rental_tracker.__main__ import self_test
from rental_tracker.services.instance_lock import AlreadyRunningError, InstanceLock


def test_only_one_copy_per_data_folder(data_dir):
    first = InstanceLock(data_dir.lock).acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            InstanceLock(data_dir.lock).acquire()
    finally:
        first.release()
    InstanceLock(data_dir.lock).acquire().release()  # free again after release


def test_self_test_reports_missing_window_library(tmp_path):
    report = tmp_path / "report.txt"
    code = self_test(report)
    text = report.read_text()
    assert "fts5 ok; files ok" in text
    if importlib.util.find_spec("webview"):
        assert code == 0 and text.strip().endswith("ok")
    else:
        assert code == 1 and "ModuleNotFoundError" in text  # dev machines without pywebview
