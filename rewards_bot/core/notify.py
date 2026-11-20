"""Gửi kết quả mỗi lượt chạy qua Telegram và / hoặc Discord.

Token bot Telegram và địa chỉ webhook Discord là bí mật: lưu mã hóa DPAPI trong data/secrets.json
(credentials.set_secret), không ghi vào settings.json, không gửi lại giao diện, không ghi ra nhật ký.
Chỉ gửi tới api.telegram.org và discord.com; địa chỉ được dựng lại từ phần đã kiểm tra, không lấy nguyên chuỗi người dùng nhập.
Lỗi gửi chỉ được ghi vào nhật ký, không bao giờ làm hỏng lượt chạy.
"""

import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .. import APP_NAME, __version__
from . import credentials

MODES = ("off", "always", "errors")  # cài đặt notify_mode: tắt / mọi lượt chạy / chỉ khi có lỗi
TG_KEY, DC_KEY = "telegram_token", "discord_webhook"
TIMEOUT = 15
TG_MAX, DC_MAX = 4000, 1900  # giới hạn độ dài tin của Telegram (4096) và Discord (2000), chừa chỗ

_TG_TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,60}")
_CHAT_ID_RE = re.compile(r"-?\d{1,20}|@[A-Za-z0-9_]{5,32}")
_DC_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_DC_PATH_RE = re.compile(r"/api/webhooks/(\d{5,25})/([A-Za-z0-9_-]{20,100})")

# khóa trạng thái Runner giữ lại cho tin tóm tắt (lần cuối mỗi tài khoản gửi lên giao diện)
KEPT = ("state", "progress", "start", "points", "today", "tasks", "streak")
OK_STATES = {"Hoàn thành"}
STOP_STATES = {"Đã dừng"}


# ---------------------------------------------------------------- kiểm tra giá trị người dùng nhập
def clean_telegram_token(value) -> str:
    s = str(value or "").strip()
    if not _TG_TOKEN_RE.fullmatch(s):
        raise ValueError("Token bot Telegram không đúng dạng, sao chép lại từ @BotFather")
    return s


def clean_chat_id(value) -> str:
    s = str(value or "").strip()
    if s and not _CHAT_ID_RE.fullmatch(s):
        raise ValueError("Chat ID Telegram phải là một số hoặc @tên_kênh")
    return s


def clean_discord_webhook(value) -> str:
    s = str(value or "").strip()
    u = urlparse(s)
    m = _DC_PATH_RE.fullmatch(u.path.rstrip("/"))
    if u.scheme != "https" or (u.hostname or "").lower() not in _DC_HOSTS or u.port or u.username or not m:
        raise ValueError("Webhook Discord không đúng dạng, sao chép lại trong Discord (Cài đặt kênh > Tích hợp > Webhook)")
    return f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"


# ---------------------------------------------------------------- nội dung tin
def _texts(lang: str) -> dict:
    if lang == "en":
        return {"pts": "points", "total": "Total gained", "stopped": "stopped", "unknown": "did not finish",
                "test": "Test message: notifications are working.", "today": "today", "other": "Other",
                "missing": "Not done", "more": "and # more", "streak": "Daily streak # days", "stamps": "Stamps"}
    return {"pts": "điểm", "total": "Tổng được", "stopped": "đã dừng", "unknown": "không chạy xong",
            "test": "Tin nhắn thử: thông báo đã hoạt động.", "today": "hôm nay", "other": "Khác",
            "missing": "Còn thiếu", "more": "và # việc khác", "streak": "Chuỗi # ngày", "stamps": "Stamp"}


# dòng "Your activity" trùng với nhóm đã tóm tắt ở trên (Search, Daily Set): không lặp lại
_SKIP_ACTIVITY = re.compile(r"^(bing|daily set)\s*[–-]", re.I)
_SHORT = {"mobile app – check-in": "Check-in"}  # tên dòng Rewards dài -> tên gọn trong tin nhắn
def _short(title: str) -> str:
    return _SHORT.get(title.lower(), re.sub(r"\s*[–-]\s*Activity$", "", title))


MISSING_MAX = 4  # số việc còn thiếu ghi tên ra, còn lại gộp "và N việc khác"


def _num(x) -> int:
    return x if isinstance(x, int) and not isinstance(x, bool) else 0


def _task_lines(r: dict, t: dict) -> list:
    """Các dòng chi tiết của một tài khoản: search, Daily Set / Keep earning / Quests, việc khác, việc còn thiếu, chuỗi."""
    rows = [x for x in (r.get("tasks") or []) if isinstance(x, dict)]
    out = []
    pts = r.get("points")
    if isinstance(r.get("today"), int) and not (isinstance(pts, int) and isinstance(r.get("start"), int)
                                                and pts - r["start"] == r["today"]):
        out.append(f"{t['today']} +{r['today']:,} {t['pts']}")
    search = [f"{x.get('title', '').replace('Search ', '')} {_num(x.get('progress'))}/{_num(x.get('max'))}"
              for x in rows if x.get("group") == "Tìm kiếm"]
    if search:
        out.append("Search: " + " · ".join(search))
    groups = []
    for g in ("Daily Set", "Keep earning", "Quests"):
        gr = [x for x in rows if x.get("group") == g]
        if gr:
            groups.append(f"{g} {sum(1 for x in gr if x.get('done'))}/{len(gr)}")
    if groups:
        out.append(" · ".join(groups))
    other = []
    for x in rows:
        title = str(x.get("title") or "")
        if x.get("group") not in ("Hoạt động", "Task thêm") or _SKIP_ACTIVITY.match(title):
            continue
        title = _short(title)[:40]
        p, m = _num(x.get("progress")), _num(x.get("max"))
        other.append(f"{title} {'✓' if x.get('done') else '✗'}" if m <= 1 else f"{title} {p}/{m}")
    if other:
        out.append(f"{t['other']}: " + " · ".join(other))
    missing = []
    for x in rows:
        if x.get("done") or x.get("group") == "Tìm kiếm" and _num(x.get("max")) == 0:
            continue
        title = _short(str(x.get("title") or ""))
        title = title if len(title) <= 40 else title[:39] + "…"
        m = _num(x.get("max"))
        missing.append(f"{title} {_num(x.get('progress'))}/{m}" if m > 1 else title)
    if missing:
        extra = len(missing) - MISSING_MAX
        out.append(f"{t['missing']}: " + "; ".join(missing[:MISSING_MAX])
                   + (f"; {t['more'].replace('#', str(extra))}" if extra > 0 else ""))
    st = r.get("streak") if isinstance(r.get("streak"), dict) else {}
    bits = []
    if _num(st.get("daily_streak")):
        bits.append(t["streak"].replace("#", str(st["daily_streak"])))
    stamps = st.get("stamps") if isinstance(st.get("stamps"), dict) else {}
    if _num(stamps.get("max")):
        bits.append(f"{t['stamps']} {_num(stamps.get('progress'))}/{stamps['max']}")
    if bits:
        out.append(" · ".join(bits))
    return out


def summary(results: dict, mode: str, lang: str = "vi", now=None):
    """Tin tóm tắt một lượt chạy, hoặc None nếu không cần gửi.

    results: {tên tài khoản: {"state", "progress", "start", "points"}} (trạng thái cuối mỗi tài khoản)."""
    if mode not in ("always", "errors") or not results:
        return None
    if all(r.get("state") in STOP_STATES for r in results.values()):
        return None  # người dùng tự bấm Dừng tất cả: không báo
    bad = [n for n, r in results.items() if r.get("state") not in OK_STATES | STOP_STATES]
    if mode == "errors" and not bad:
        return None
    t = _texts(lang)
    lines = [f"{APP_NAME} · {time.strftime('%d/%m %H:%M', now or time.localtime())}"]
    total = 0
    for name in sorted(results, key=str.lower):
        r = results[name]
        state, progress = r.get("state") or "", str(r.get("progress") or "").strip()
        pts = r.get("points")
        if isinstance(pts, int) and isinstance(r.get("start"), int):
            total += max(0, pts - r["start"])
        icon = "✅" if state in OK_STATES else "⏹" if state in STOP_STATES else "❌"
        if state in OK_STATES:
            detail = progress or "OK"
        elif state in STOP_STATES:
            detail = t["stopped"]
        else:
            detail = " · ".join(x for x in (state if state != "Lỗi" else "", progress) if x) or t["unknown"]
        tail = f" · {pts:,} {t['pts']}" if isinstance(pts, int) else ""
        lines.append(f"{icon} {name}: {detail}{tail}")
        if state not in STOP_STATES:
            lines += [f"   {x}" for x in _task_lines(r, t)]
        lines.append("")
    if total:
        lines.append(f"{t['total']}: +{total:,} {t['pts']}")
    return "\n".join(lines)


def test_text(lang: str = "vi") -> str:
    return f"{APP_NAME}: {_texts(lang)['test']}"


# ---------------------------------------------------------------- gửi
def channels(chat_id: str = "") -> dict:
    """Kênh đã cấu hình: {"telegram": (token, chat_id)} và / hoặc {"discord": url}. Không đọc được token thì bỏ qua."""
    out = {}
    tg = credentials.get_secret(TG_KEY)
    if tg and chat_id:
        out["telegram"] = (tg, chat_id)
    dc = credentials.get_secret(DC_KEY)
    if dc:
        out["discord"] = dc
    return out


def _post(url: str, payload: dict, opener) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          # Discord (Cloudflare) chặn User-Agent mặc định của urllib
                                          "User-Agent": f"{APP_NAME}/{__version__}"})
    with opener(req, timeout=TIMEOUT) as r:
        r.read()


def _why(e: Exception) -> str:
    """Lý do lỗi ngắn gọn, không chứa địa chỉ (địa chỉ Telegram có token, địa chỉ Discord có mã webhook)."""
    if isinstance(e, urllib.error.HTTPError):
        desc = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            desc = str(body.get("description") or body.get("message") or "")[:120]
        except (ValueError, OSError, AttributeError):
            pass
        return f"HTTP {e.code}" + (f": {desc}" if desc else "")
    if isinstance(e, urllib.error.URLError):
        return f"không kết nối được ({type(e.reason).__name__})"
    return type(e).__name__


def send(text: str, chans: dict, opener=urllib.request.urlopen) -> dict:
    """Gửi text tới các kênh. Trả {kênh: "" nếu được, hoặc lý do lỗi}."""
    out = {}
    if "telegram" in chans:
        token, chat = chans["telegram"]
        try:
            _post(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat, "text": text[:TG_MAX], "disable_web_page_preview": True}, opener)
            out["telegram"] = ""
        except Exception as e:  # noqa: BLE001 (gửi thông báo không được làm hỏng lượt chạy)
            out["telegram"] = _why(e)
    if "discord" in chans:
        try:
            _post(chans["discord"], {"content": text[:DC_MAX], "allowed_mentions": {"parse": []}}, opener)
            out["discord"] = ""
        except Exception as e:  # noqa: BLE001
            out["discord"] = _why(e)
    return out
