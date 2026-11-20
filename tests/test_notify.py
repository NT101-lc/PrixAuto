import io
import json
import sys
import threading
import urllib.error

import pytest

from rewards_bot.core import credentials, notify, runner as R

TOKEN = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
HOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyzABCDEF_-12"


# ---------------------------------------------------------------- kiểm tra giá trị nhập
@pytest.mark.parametrize("value,ok", [
    (TOKEN, True), (f"  {TOKEN} ", True), ("", False), ("abc", False), ("123:short", False),
    (TOKEN + "/../x", False), (TOKEN + "\n", True),
])
def test_telegram_token(value, ok):
    if ok:
        assert notify.clean_telegram_token(value) == TOKEN
    else:
        with pytest.raises(ValueError):
            notify.clean_telegram_token(value)


@pytest.mark.parametrize("value,ok", [
    ("123456789", True), ("-1001234567890", True), ("@my_channel", True), ("", True),
    ("12a", False), ("@ab", False), ("1; drop", False), ("-" * 3, False),
])
def test_chat_id(value, ok):
    if ok:
        assert notify.clean_chat_id(value) == value
    else:
        with pytest.raises(ValueError):
            notify.clean_chat_id(value)


@pytest.mark.parametrize("value,ok", [
    (HOOK, True), (HOOK + "/", True), (HOOK.replace("discord.com", "discordapp.com"), True),
    (HOOK + "?wait=true", True),  # query bị bỏ khi dựng lại địa chỉ
    (HOOK.replace("https", "http"), False), (HOOK.replace("discord.com", "evil.com"), False),
    (HOOK.replace("discord.com", "discord.com.evil.com"), False), (HOOK.replace("discord.com", "discord.com:8443"), False),
    (HOOK.replace("https://", "https://user@"), False), ("https://discord.com/api/webhooks/1/x", False), ("", False),
])
def test_discord_webhook(value, ok):
    if ok:
        assert notify.clean_discord_webhook(value) == HOOK
    else:
        with pytest.raises(ValueError):
            notify.clean_discord_webhook(value)


# ---------------------------------------------------------------- tin tóm tắt
OK = {"state": "Hoàn thành", "progress": "+226", "start": 3038, "points": 3264}
BAD = {"state": "Lỗi", "progress": "+180 · Search Mobile không được cộng điểm", "start": 5000, "points": 5180}
STOP = {"state": "Đã dừng", "progress": ""}


def test_summary_always_lists_every_account_and_total():
    text = notify.summary({"namanh": BAD, "mkvison": OK}, "always")
    lines = text.splitlines()
    assert lines[1] == "✅ mkvison: +226 · 3,264 điểm"  # xếp theo tên
    assert lines[3] == "❌ namanh: +180 · Search Mobile không được cộng điểm · 5,180 điểm"
    assert lines[-1] == "Tổng được: +406 điểm"


TASKS = [
    {"group": "Tìm kiếm", "title": "Search PC", "progress": 87, "max": 90, "done": False},
    {"group": "Tìm kiếm", "title": "Search Mobile", "progress": 60, "max": 60, "done": True},
    *[{"group": "Daily Set", "title": f"d{i}", "progress": 10, "max": 10, "done": True} for i in range(3)],
    {"group": "Keep earning", "title": "Quote of the day", "progress": 0, "max": 5, "done": False},
    {"group": "Quests", "title": "Rewards App weekly Exclusive Quest with a very long name", "progress": 1, "max": 8,
     "done": False},
    {"group": "Hoạt động", "title": "Bing – Search", "progress": 1, "max": 1, "done": True},
    {"group": "Hoạt động", "title": "Daily Set – Activity", "progress": 3, "max": 3, "done": True},
    {"group": "Hoạt động", "title": "Mobile App – Check-in", "progress": 1, "max": 1, "done": True},
    {"group": "Hoạt động", "title": "Visual Search – Activity", "progress": 0, "max": 1, "done": False},
    {"group": "Hoạt động", "title": "Read to Earn", "progress": 30, "max": 30, "done": True},
    {"group": "Task thêm", "title": "Browse", "progress": 0, "max": 30, "done": False},
]


def test_summary_lists_what_was_done():
    r = {**OK, "today": 300, "tasks": TASKS, "streak": {"daily_streak": 40, "stamps": {"progress": 3, "max": 12}}}
    lines = notify.summary({"a": r}, "always").splitlines()
    assert lines[2:] == [
        "   hôm nay +300 điểm",  # khác số được ở lượt này (đã chạy một lượt khác trong ngày)
        "   Search: PC 87/90 · Mobile 60/60",
        "   Daily Set 3/3 · Keep earning 0/1 · Quests 0/1",
        "   Khác: Check-in ✓ · Visual Search ✗ · Read to Earn 30/30 · Browse 0/30",
        "   Còn thiếu: Search PC 87/90; Quote of the day 0/5; Rewards App weekly Exclusive Quest with… 1/8; "
        "Visual Search; và 1 việc khác",
        "   Chuỗi 40 ngày · Stamp 3/12",
        "",
        "Tổng được: +226 điểm",
    ]


def test_summary_survives_odd_task_data():
    r = {**OK, "tasks": [None, "x", {"group": "Tìm kiếm"}, {"group": "Quests", "progress": "?", "max": None}],
         "streak": "bad", "today": True}
    assert notify.summary({"a": r}, "always")


def test_summary_modes():
    assert notify.summary({"a": OK}, "off") is None
    assert notify.summary({"a": OK}, "errors") is None  # không có lỗi
    assert notify.summary({"a": OK, "b": BAD}, "errors")
    assert notify.summary({"a": STOP, "b": STOP}, "always") is None  # tự bấm Dừng tất cả
    assert notify.summary({}, "always") is None
    assert notify.summary({"a": OK}, "evil") is None


def test_summary_not_logged_in_and_unfinished_count_as_errors():
    text = notify.summary({"a": {"state": "Chưa đăng nhập", "progress": "Bấm Đăng nhập"}, "b": {}}, "errors")
    assert "❌ a: Chưa đăng nhập · Bấm Đăng nhập" in text
    assert "❌ b: không chạy xong" in text
    assert "Tổng" not in text


def test_summary_english():
    text = notify.summary({"a": OK, "b": STOP}, "always", "en")
    assert "3,264 points" in text and "⏹ b: stopped" in text and "Total gained: +226 points" in text


# ---------------------------------------------------------------- gửi
class FakeResp:
    def __init__(self):
        self.read = lambda: b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_builds_requests():
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, json.loads(req.data), req.get_header("User-agent"), timeout))
        return FakeResp()

    out = notify.send("x" * 5000, {"telegram": (TOKEN, "42"), "discord": HOOK}, opener)
    assert out == {"telegram": "", "discord": ""}
    (tg_url, tg, ua, timeout), (dc_url, dc, _, _) = seen
    assert tg_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and tg["chat_id"] == "42"
    assert len(tg["text"]) == notify.TG_MAX and timeout == notify.TIMEOUT
    assert ua.startswith("PrixAuto/")  # Discord chặn User-Agent mặc định của urllib
    assert dc_url == HOOK and len(dc["content"]) == notify.DC_MAX and dc["allowed_mentions"] == {"parse": []}


def test_send_errors_never_leak_secrets():
    def opener(req, timeout):
        if "telegram" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))
        raise urllib.error.URLError(OSError(f"cannot reach {req.full_url}"))

    out = notify.send("hi", {"telegram": (TOKEN, "42"), "discord": HOOK}, opener)
    assert out["telegram"] == "HTTP 401: Unauthorized"
    assert out["discord"].startswith("không kết nối được")
    assert TOKEN not in json.dumps(out) and "abcdefghij" not in json.dumps(out)


# ---------------------------------------------------------------- bí mật (DPAPI)
@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI chỉ có trên Windows")
def test_secret_roundtrip(data_dir):
    from rewards_bot.core import config
    assert credentials.get_secret("telegram_token") is None and not credentials.has_secret("telegram_token")
    credentials.set_secret("telegram_token", TOKEN)
    assert TOKEN not in config.SECRETS_FILE.read_text(encoding="utf-8")  # chỉ lưu bản mã hóa
    assert credentials.get_secret("telegram_token") == TOKEN and credentials.has_secret("telegram_token")
    credentials.remove_secret("telegram_token")
    assert credentials.get_secret("telegram_token") is None


# ---------------------------------------------------------------- Runner gom kết quả, gửi 1 tin khi tất cả xong
class FakeBot:
    def __init__(self, prof, settings, bank, log, status, stop):
        self.name, self._status = prof["name"], status

    def run(self):
        self._status(self.name, {"start": 100, "state": "Đang chạy"})
        self._status(self.name, {"points": 130})
        ok = self.name != "b"
        self._status(self.name, {"state": "Hoàn thành" if ok else "Lỗi", "progress": "+30" if ok else "+30 · lỗi"})

    def checkin_only(self):
        self._status(self.name, {"state": "Hoàn thành", "progress": ""})

    def diagnose(self):
        self._status(self.name, {"state": "Sẵn sàng"})


def _runner(monkeypatch, settings, action="run"):
    monkeypatch.setattr(R, "RewardsBot", FakeBot)
    from rewards_bot.core import config
    monkeypatch.setattr(R, "LOG_DIR", config.LOG_DIR)  # data_dir đã đổi sang thư mục tạm
    monkeypatch.setattr(R.notify, "channels", lambda chat: {"discord": HOOK})
    finished, order, sent = threading.Event(), [], []
    done_names = set()

    def done(n):
        order.append(("done", n))
        done_names.add(n)
        if done_names == {"a", "b"}:
            finished.set()

    r = R.Runner(bank=None, log=lambda n, m: order.append(("log", m)), status=lambda n, d: None, done=done)

    def send(text, chans):
        sent.append(text)
        order.append(("send", None))
        return {"discord": ""}

    r.send = send
    r.start([{"name": "a"}, {"name": "b"}], {"max_parallel": 2, "notify_mode": "always", **settings}, action=action)
    assert finished.wait(10)
    return sent, order


def test_runner_sends_one_summary_after_all_accounts(data_dir, monkeypatch):
    sent, order = _runner(monkeypatch, {})
    assert len(sent) == 1
    assert "✅ a: +30 · 130 điểm" in sent[0] and "❌ b: +30 · lỗi · 130 điểm" in sent[0] and "+60" in sent[0]
    # gửi trước khi báo xong tài khoản cuối: lệnh --run-all không thoát trước khi tin đi
    assert order.index(("send", None)) < max(i for i, e in enumerate(order) if e[0] == "done")
    assert ("log", "Đã gửi thông báo qua Discord") in order


def test_runner_respects_mode_and_action(data_dir, monkeypatch):
    assert _runner(monkeypatch, {"notify_mode": "off"})[0] == []
    assert len(_runner(monkeypatch, {"notify_mode": "errors"})[0]) == 1  # b lỗi
    assert _runner(monkeypatch, {"notify_mode": "errors"}, action="checkin")[0] == []  # check-in đều được
    assert _runner(monkeypatch, {}, action="check")[0] == []  # Kiểm tra không gửi


def test_runner_notify_failure_does_not_break_run(data_dir, monkeypatch):
    monkeypatch.setattr(R.notify, "summary", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    _, order = _runner(monkeypatch, {})
    assert ("log", "cảnh báo: không gửi được thông báo (RuntimeError)") in order


# ---------------------------------------------------------------- API giao diện
@pytest.fixture
def api(data_dir):
    W = pytest.importorskip("rewards_bot.ui.webapp")
    a = W.Api.__new__(W.Api)
    a._settings = {"profiles": [], "telegram_chat_id": "", "language": "vi"}
    return W, a


@pytest.mark.parametrize("key,value,ok", [
    ("notify_mode", "off", True), ("notify_mode", "errors", True), ("notify_mode", "evil", False),
    ("telegram_chat_id", "", True), ("telegram_chat_id", "-1001234567890", True), ("telegram_chat_id", "@my_channel", True),
    ("telegram_chat_id", "12a", False), ("telegram_chat_id", 42, False),
])
def test_notify_settings_schema(api, key, value, ok):
    W, _ = api
    if ok:
        assert W._clean_setting(key, value) == value
    else:
        with pytest.raises(ValueError):
            W._clean_setting(key, value)


def test_bad_channel_input_saves_nothing(api, monkeypatch):
    W, a = api
    monkeypatch.setattr(credentials, "supported", lambda: True)
    saved = []
    monkeypatch.setattr(credentials, "set_secret", lambda k, v: saved.append(k))
    monkeypatch.setattr(credentials, "remove_secret", lambda k: saved.append("rm " + k))
    r = a.save_notify_channels({"telegram_token": TOKEN, "discord_webhook": "https://evil.com/x", "clear": ["telegram"]})
    assert r["ok"] is False and "Discord" in r["error"] and saved == []  # kiểm tra hết rồi mới ghi / xóa
    assert a.save_notify_channels("x")["ok"] is False


def test_channel_info_never_returns_secrets(api, monkeypatch):
    W, a = api
    monkeypatch.setattr(credentials, "has_secret", lambda k: k == notify.TG_KEY)
    info = a._notify_info()
    assert info["telegram"] is True and info["discord"] is False and TOKEN not in json.dumps(info)


def test_test_notify_without_channels(api, monkeypatch):
    W, a = api
    monkeypatch.setattr(notify, "channels", lambda chat: {})
    r = a.test_notify()
    assert r["ok"] is False and "Chưa có kênh" in r["error"]
