"""Lịch chạy hằng ngày bằng Task Scheduler của Windows, bật / tắt ngay trong app (Cài đặt → Chạy tự động).

App không tự hẹn giờ: Windows mở `PrixAuto.exe --run-all --headless` mỗi ngày (lệnh này tự chờ ngẫu nhiên
0..start_jitter_max phút trước khi chạy). Task được tạo cho đúng tài khoản Windows đang dùng, không cần quyền admin:
chỉ chạy khi đã đăng nhập Windows, chạy bù nếu lỡ giờ (máy tắt), tùy chọn đánh thức máy từ Sleep.
Nguồn sự thật là chính task trong Windows (đọc lại bằng schtasks), app không lưu bản sao trong cài đặt.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .. import APP_NAME
from . import config

TASK_NAME = f"{APP_NAME} - Daily run"
ARGS = "--run-all --headless"
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: không nháy cửa sổ đen khi gọi schtasks


class ScheduleError(Exception):
    pass


def supported() -> bool:
    return sys.platform == "win32"


def command() -> tuple[str, str, str]:
    """(chương trình, tham số, thư mục chạy) cho lượt chạy theo lịch: bản .exe, hoặc pythonw khi chạy từ mã nguồn."""
    if config.FROZEN:
        return sys.executable, ARGS, str(config.BASE_DIR)
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")  # không mở cửa sổ console
    return str(quiet if quiet.exists() else exe), f"-m rewards_bot {ARGS}", str(config.BASE_DIR)


def clean_time(value) -> str:
    """'HH:MM' 24 giờ, hoặc ValueError."""
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("Giờ chạy phải có dạng HH:MM (00:00 - 23:59).")
    return value


def task_xml(time_hhmm: str, wake: bool, user: str | None = None) -> str:
    """Định nghĩa task (Task Scheduler 1.2). Mỗi giá trị đều được escape; giờ đã qua clean_time."""
    time_hhmm = clean_time(time_hhmm)
    prog, args, cwd = command()
    user = user if user is not None else _current_user()
    uid = f"<UserId>{escape(user)}</UserId>" if user else ""
    b = lambda v: "true" if v else "false"  # noqa: E731
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(APP_NAME)}: chạy mọi tài khoản mỗi ngày (tạo từ Cài đặt của app).</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2024-01-01T{time_hhmm}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{uid}<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <WakeToRun>{b(wake)}</WakeToRun>
    <ExecutionTimeLimit>PT8H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(prog)}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(cwd)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def parse_xml(xml: str) -> dict:
    """Đọc lại task đã tạo: giờ chạy, đánh thức máy, task có đang bật, và có trỏ đúng bản app này không."""
    def tag(name, src=xml):
        m = re.search(rf"<{name}>(.*?)</{name}>", src, re.S)
        return m.group(1).strip() if m else ""
    settings = re.search(r"<Settings>(.*?)</Settings>", xml, re.S)
    settings = settings.group(1) if settings else ""
    start = tag("StartBoundary")
    t = re.search(r"T(\d{2}:\d{2})", start)
    prog, _, _ = command()
    cmd = tag("Command").replace("&amp;", "&").strip('"')
    return {
        "time": t.group(1) if t else "",
        "wake": tag("WakeToRun", settings).lower() == "true",
        "task_enabled": tag("Enabled", settings).lower() != "false",  # của task (thẻ Enabled trong Triggers là của lịch)
        "matches": os.path.normcase(os.path.abspath(cmd)) == os.path.normcase(os.path.abspath(prog)) if cmd else False,
    }


def _current_user() -> str:
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    return f"{dom}\\{user}" if dom and user else user


def _run(args: list) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["schtasks", *args], capture_output=True, timeout=20,
                              creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ScheduleError(f"Không gọi được Task Scheduler: {e}") from e


def _text(b: bytes) -> str:
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16")
    for enc in ("utf-8", "mbcs" if sys.platform == "win32" else "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1")


def status() -> dict:
    """{"supported", "enabled", "time", "wake", "task_enabled", "matches"}. Không có task thì enabled = False."""
    if not supported():
        return {"supported": False, "enabled": False}
    r = _run(["/Query", "/TN", TASK_NAME, "/XML"])
    if r.returncode != 0:
        return {"supported": True, "enabled": False}
    return {"supported": True, "enabled": True, **parse_xml(_text(r.stdout))}


def enable(time_hhmm: str, wake: bool) -> dict:
    """Tạo (hoặc ghi đè) task chạy mỗi ngày lúc time_hhmm."""
    if not supported():
        raise ScheduleError("Chỉ hẹn giờ được trên Windows. Trên Linux dùng cron: ./scripts/run.sh --run-all --headless")
    xml = task_xml(time_hhmm, bool(wake))
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="prixauto-task-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(xml.encode("utf-16"))  # schtasks /XML cần UTF-16 có BOM
        r = _run(["/Create", "/TN", TASK_NAME, "/XML", path, "/F"])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if r.returncode != 0:
        raise ScheduleError("Windows không tạo được lịch chạy: " + (_text(r.stderr or r.stdout).strip() or f"mã {r.returncode}"))
    return status()


def disable() -> dict:
    """Xóa task (không có task thì coi như đã tắt)."""
    if supported():
        r = _run(["/Delete", "/TN", TASK_NAME, "/F"])
        if r.returncode != 0 and status().get("enabled"):
            raise ScheduleError("Windows không xóa được lịch chạy: " + _text(r.stderr or r.stdout).strip())
    return status()
