"""Tự làm mới điểm: khi mở app (tài khoản chưa đọc điểm trong hôm nay) và sau khi chạy xong một tài khoản."""
from datetime import date, timedelta

from test_ui_smoke import app_page, boot_n, server  # noqa: F401  (fixture server dùng chung)



TODAY, YESTERDAY = date.today(), date.today() - timedelta(days=1)


def boot(updated, **settings):
    """updated: mỗi tài khoản acc0.. một ngày đọc điểm gần nhất (None = chưa từng đọc được)."""
    b = boot_n(len(updated))
    for i, d in enumerate(updated):
        if d is None:
            b["pstate"][f"acc{i}"].pop("updated", None)
        else:
            b["pstate"][f"acc{i}"]["updated"] = f"09:00 {d:%d/%m}"
    b["settings"].update(settings)
    return b


REFRESHES = "window.__calls.filter((c) => c[0] === 'start' && c[2] === 'refresh').map((c) => c[1][0])"


def test_refresh_on_open_only_stale_accounts_in_a_queue(server):
    # acc0 đã đọc hôm nay, acc1 + acc2 đọc hôm qua, acc3 chưa từng đọc được (chưa đăng nhập) -> chỉ acc1, acc2;
    # max_parallel 1 nên acc2 chỉ bắt đầu khi acc1 xong
    with app_page(server, boot([TODAY, YESTERDAY, YESTERDAY, None], max_parallel=1)) as (pg, errors):
        pg.wait_for_timeout(300)
        assert pg.evaluate(REFRESHES) == ["acc1"]
        assert "Tự làm mới điểm 2 tài khoản" in pg.evaluate("S.logs.map((l) => l.msg).join('\\n')")
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc1'}])")
        pg.wait_for_timeout(200)
        assert pg.evaluate(REFRESHES) == ["acc1", "acc2"]
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc2'}])")
        pg.wait_for_timeout(200)
        assert pg.evaluate(REFRESHES) == ["acc1", "acc2"]
        assert pg.evaluate("AutoRefresh._state()") == {"queue": [], "active": [], "timers": []}
        assert errors == []


def test_refresh_on_open_respects_switch_and_parallel_limit(server):
    with app_page(server, boot([YESTERDAY, YESTERDAY], auto_refresh_open=False)) as (pg, errors):
        pg.wait_for_timeout(300)
        assert pg.evaluate(REFRESHES) == []
    with app_page(server, boot([YESTERDAY] * 4, max_parallel=3)) as (pg, errors):
        pg.wait_for_timeout(300)
        assert pg.evaluate(REFRESHES) == ["acc0", "acc1", "acc2"]
        assert errors == []


def test_refresh_after_run_waits_then_refreshes_that_account(server):
    with app_page(server, boot([TODAY, TODAY])) as (pg, errors):
        pg.clock.install()
        pg.evaluate("start(['acc0'], 'run')")
        pg.evaluate("start(['acc1'], 'refresh')")  # làm mới tay: xong thì không tự làm mới thêm
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc0'}, {type: 'done', name: 'acc1'}])")
        assert pg.evaluate("AutoRefresh._state().timers") == ["acc0"]
        assert pg.evaluate(REFRESHES) == ["acc1"]  # chỉ lần bấm tay, chưa tự làm mới ngay
        pg.clock.run_for("02:50")
        assert pg.evaluate(REFRESHES) == ["acc1"]
        pg.clock.run_for("00:15")  # đủ 3 phút
        assert pg.evaluate(REFRESHES) == ["acc1", "acc0"]
        # chạy lại trước khi hết giờ thì hủy lần tự làm mới cũ
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc0'}])")  # xong lượt tự làm mới
        pg.evaluate("start(['acc1'], 'everything')")
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc1'}])")
        assert pg.evaluate("AutoRefresh._state().timers") == ["acc1"]
        pg.evaluate("start(['acc1'], 'run')")
        assert pg.evaluate("AutoRefresh._state().timers") == []
        assert errors == []


def test_refresh_after_run_switch_off_and_settings_rows(server):
    with app_page(server, boot([TODAY], auto_refresh_after_run=False)) as (pg, errors):
        pg.evaluate("start(['acc0'], 'run')")
        pg.evaluate("window.onEvents([{type: 'done', name: 'acc0'}])")
        assert pg.evaluate("AutoRefresh._state().timers") == []
        # Cài đặt → Dữ liệu: 2 công tắc, lưu gửi đúng giá trị
        pg.evaluate("go('settings')")
        assert pg.is_checked("#set-auto-refresh-open") and not pg.is_checked("#set-auto-refresh-after-run")
        pg.click("label:has(#set-auto-refresh-open)")
        pg.click("#btn-save-settings")
        pg.wait_for_timeout(200)
        saved = pg.evaluate("window.__calls.filter((c) => c[0] === 'save_settings').map((c) => c[1]).pop()")
        assert saved["auto_refresh_open"] is False and saved["auto_refresh_after_run"] is False
        assert errors == []


def test_settings_schema_accepts_auto_refresh_keys():
    from rewards_bot.core.config import DEFAULT_SETTINGS
    from rewards_bot.ui.webapp import SETTING_SCHEMA
    for k in ("auto_refresh_open", "auto_refresh_after_run"):
        assert SETTING_SCHEMA[k] == (bool,) and DEFAULT_SETTINGS[k] is True
