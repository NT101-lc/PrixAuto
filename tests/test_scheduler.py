"""Lịch chạy hằng ngày (Task Scheduler): định nghĩa task, đọc lại task, kiểm tra dữ liệu từ giao diện.
Không tạo task thật: các lệnh schtasks được thay bằng hàm giả."""
import subprocess
import sys

import pytest

from rewards_bot.core import config, scheduler as sch
from rewards_bot.ui import webapp as W


@pytest.mark.parametrize("value, ok", [("09:00", True), ("23:59", True), ("00:00", True), ("24:00", False), ("9:00", False),
                                       ("09:60", False), ("", False), (None, False), (900, False), ("09:00 & calc", False)])
def test_clean_time(value, ok):
    if ok:
        assert sch.clean_time(value) == value
    else:
        with pytest.raises(ValueError):
            sch.clean_time(value)


def test_task_xml_daily_trigger_safe_defaults_and_escaped(monkeypatch, tmp_path):
    odd = tmp_path / "A&B <x>"
    monkeypatch.setattr(sch, "command", lambda: (str(odd / "PrixAuto.exe"), sch.ARGS, str(odd)))
    xml = sch.task_xml("07:30", wake=True, user="PC\\me")
    assert "<StartBoundary>2024-01-01T07:30:00</StartBoundary>" in xml and "<DaysInterval>1</DaysInterval>" in xml
    assert "<WakeToRun>true</WakeToRun>" in xml and "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml and "<RunLevel>LeastPrivilege</RunLevel>" in xml  # không cần mật khẩu, không admin
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml  # laptop chạy pin vẫn chạy
    assert "<Arguments>--run-all --headless</Arguments>" in xml
    assert "A&amp;B &lt;x&gt;" in xml and "A&B <x>" not in xml
    assert "<UserId>PC\\me</UserId>" in xml
    assert "<WakeToRun>false</WakeToRun>" in sch.task_xml("07:30", wake=False, user="")
    with pytest.raises(ValueError):
        sch.task_xml("7:30", wake=False)


def test_parse_xml_roundtrip_and_detects_moved_app(monkeypatch):
    monkeypatch.setattr(sch, "command", lambda: (r"C:\PrixAuto\PrixAuto.exe", sch.ARGS, r"C:\PrixAuto"))
    st = sch.parse_xml(sch.task_xml("21:15", wake=True, user=""))
    assert st == {"time": "21:15", "wake": True, "task_enabled": True, "matches": True}
    moved = sch.task_xml("21:15", wake=False, user="").replace(r"C:\PrixAuto\PrixAuto.exe", r"D:\Old\PrixAuto.exe")
    assert sch.parse_xml(moved)["matches"] is False
    assert sch.parse_xml(moved.replace("<Enabled>true</Enabled>\n  </Settings>", "<Enabled>false</Enabled>\n  </Settings>"))["task_enabled"] is False


def test_command_exe_vs_source(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PrixAuto.exe"))
    assert sch.command()[0].endswith("PrixAuto.exe") and sch.command()[1] == "--run-all --headless"
    monkeypatch.setattr(config, "FROZEN", False)
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    prog, args, _ = sch.command()
    assert prog.endswith("pythonw.exe") and args == "-m rewards_bot --run-all --headless"


def test_enable_writes_utf16_xml_and_disable_deletes(monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "/Create":
            data = open(args[args.index("/XML") + 1], "rb").read()
            assert data.startswith(b"\xff\xfe") and "<WakeToRun>true</WakeToRun>" in data.decode("utf-16")
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[0] == "/Query":
            return subprocess.CompletedProcess(args, 0, sch.task_xml("06:45", True, "").encode("utf-16"), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(sch, "supported", lambda: True)
    monkeypatch.setattr(sch, "_run", fake_run)
    st = sch.enable("06:45", True)
    assert st["enabled"] and st["time"] == "06:45" and st["wake"]
    assert calls[0][:3] == ["/Create", "/TN", sch.TASK_NAME] and calls[0][-1] == "/F"
    sch.disable()
    assert ["/Delete", "/TN", sch.TASK_NAME, "/F"] in calls


def test_enable_reports_windows_error(monkeypatch):
    monkeypatch.setattr(sch, "supported", lambda: True)
    monkeypatch.setattr(sch, "_run", lambda a: subprocess.CompletedProcess(a, 1, b"", b"ERROR: Access is denied."))
    with pytest.raises(sch.ScheduleError, match="Access is denied"):
        sch.enable("09:00", False)


def test_status_without_task_or_off_windows(monkeypatch):
    monkeypatch.setattr(sch, "supported", lambda: True)
    monkeypatch.setattr(sch, "_run", lambda a: subprocess.CompletedProcess(a, 1, b"", b"ERROR: not found"))
    assert sch.status() == {"supported": True, "enabled": False}
    monkeypatch.setattr(sch, "supported", lambda: False)
    assert sch.status() == {"supported": False, "enabled": False}
    with pytest.raises(sch.ScheduleError):
        sch.enable("09:00", False)


@pytest.mark.parametrize("data", [None, "x", {}, {"enabled": "yes"}, {"enabled": True, "time": "25:00"},
                                  {"enabled": True, "time": "09:00; del"}])
def test_api_set_schedule_rejects_bad_input(monkeypatch, data):
    monkeypatch.setattr(sch, "enable", lambda *a: pytest.fail("không được tạo task với dữ liệu sai"))
    r = W.Api.set_schedule(object.__new__(W.Api), data)
    assert r["ok"] is False and r["error"]


def test_api_set_schedule_calls_scheduler(monkeypatch):
    got = []
    monkeypatch.setattr(sch, "enable", lambda t, w: got.append((t, w)) or {"supported": True, "enabled": True, "time": t})
    monkeypatch.setattr(sch, "disable", lambda: got.append("off") or {"supported": True, "enabled": False})
    api = object.__new__(W.Api)
    assert W.Api.set_schedule(api, {"enabled": True, "time": "08:05", "wake": True})["schedule"]["time"] == "08:05"
    assert W.Api.set_schedule(api, {"enabled": True, "time": "08:05", "wake": "true"})["ok"]  # chỉ True thật mới bật đánh thức
    assert W.Api.set_schedule(api, {"enabled": False})["schedule"]["enabled"] is False
    assert got == [("08:05", True), ("08:05", False), "off"]
