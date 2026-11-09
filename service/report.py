"""
รายงาน "งานที่อาจถูกลืม" สำหรับ Plane — ใช้เป็น module ของ service

- ดึงข้อมูลจาก Plane API
- ตรวจกฎ: overdue / no assignee / no due / stale / aging
- สร้าง HTML แล้วโพสต์เป็นคอมเมนต์ใน issue ที่กำหนด (THZI-8 = Daily Standup)

ไม่ได้ใช้ AI — ไม่กินเครดิต
"""

from __future__ import annotations

import html
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("plane-daily")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


_ENV = _load_dotenv(os.path.join(ROOT, ".env"))
_ENV.update(_load_dotenv(os.path.join(os.path.dirname(ROOT), ".env")))


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or _ENV.get(name) or default).strip()


PLANE_URL = env("PLANE_URL").rstrip("/")
PLANE_WORKSPACE = env("PLANE_WORKSPACE", "thzi-gtm")
PLANE_API_KEY = env("PLANE_API_KEY")
PLANE_PROJECT_ID = env("PLANE_PROJECT_ID")
HUB_ISSUE_ID = env("HUB_ISSUE_ID", "90d0f8e0-2724-4a40-8bb8-41ba12b4d0c4")
STALE_DAYS = int(env("STALE_DAYS", "5") or 5)
AGING_DAYS = int(env("AGING_DAYS", "14") or 14)
TZ = ZoneInfo(env("REPORT_TIMEZONE", "Asia/Bangkok"))
REPORT_AT = env("DAILY_REPORT_AT", "08:30")


# --------------------------------------------------------------------------
# Plane API
# --------------------------------------------------------------------------

HEADERS = {
    "X-API-Key": "",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "plane-daily-report/1.0",
}


def _api(path: str, payload: dict | None = None, method: str = "GET"):
    HEADERS["X-API-Key"] = PLANE_API_KEY
    url = f"{PLANE_URL}/api/v1/{path.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _days_since(value) -> int | None:
    d = _as_date(value)
    return None if d is None else (datetime.now(TZ).date() - d).days


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def collect() -> dict:
    """ดึงข้อมูลและประเมินกฎ — คืนโครงที่พร้อม render"""
    project = {}
    try:
        project = _api(f"workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/")
    except urllib.error.HTTPError:
        for p in _api(f"workspaces/{PLANE_WORKSPACE}/projects/").get("results", []):
            if p.get("id") == PLANE_PROJECT_ID:
                project = p
                break

    states = {
        s["id"]: s
        for s in _api(f"workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/states/")["results"]
    }
    labels = {
        l["id"]: l["name"]
        for l in _api(f"workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/labels/")["results"]
    }
    members_raw = _api(f"workspaces/{PLANE_WORKSPACE}/members/")
    members = {
        m["id"]: (m.get("first_name") or m.get("display_name") or "?")
        for m in (members_raw if isinstance(members_raw, list) else members_raw.get("results", []))
    }
    items = _api(
        f"workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/work-items/?per_page=100"
    ).get("results", [])

    prefix = project.get("identifier") or "ITEM"
    today = datetime.now(TZ).date()
    flags: list[dict] = []

    for it in items:
        if it.get("archived_at"):
            continue
        st = states.get(it.get("state")) or {}
        group = (st.get("group") or "").lower()
        if group in ("completed", "cancelled"):
            continue
        if group == "backlog":
            continue  # งานที่ยังไม่เริ่ม ไม่ต้องเตือน (กัน noise)

        reasons: list[str] = []
        severity = "info"

        target = _as_date(it.get("target_date"))
        if target and target < today:
            reasons.append(
                f"เลยกำหนด {target.strftime('%d/%m/%Y')} มา {(today - target).days} วัน"
            )
            severity = "high"

        if not (it.get("assignees") or []):
            reasons.append("ไม่มีผู้รับผิดชอบ")
            severity = "high"

        if group == "started" and not target:
            reasons.append("กำลังทำแต่ไม่มีกำหนดเสร็จ")
            severity = "high" if severity == "high" else "med"

        upd = _days_since(it.get("updated_at"))
        if upd is not None and upd > STALE_DAYS:
            reasons.append(f"ไม่ขยับมา {upd} วัน")
            severity = "high" if upd > STALE_DAYS * 2 else ("high" if severity == "high" else "med")

        age = _days_since(it.get("start_date") or it.get("created_at"))
        if group == "started" and age is not None and age > AGING_DAYS:
            reasons.append(f"ค้างสถานะนี้ {age} วัน")
            if severity != "high":
                severity = "med"

        if reasons:
            assignee_names = [
                members.get(a, str(a)[:8]) for a in (it.get("assignees") or [])
            ]
            flags.append(
                {
                    "ref": f"{prefix}-{it.get('sequence_id', '?')}",
                    "name": it.get("name", "?"),
                    "state": st.get("name") or "?",
                    "who": ", ".join(assignee_names) or "— ไม่มีเจ้าภาพ —",
                    "reasons": reasons,
                    "severity": severity,
                    "link": f"{PLANE_URL}/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/issues/{it['id']}/",
                    "labels": [labels.get(l, l) for l in (it.get("labels") or [])],
                }
            )

    order = {"high": 0, "med": 1, "info": 2}
    flags.sort(key=lambda f: (order[f["severity"]], f["ref"]))
    return {"project": project.get("name", ""), "prefix": prefix, "date": today, "flags": flags}


def render_html(data: dict) -> str:
    flags = data["flags"]
    if not flags:
        return (
            f"<p><strong>📋 ตรวจงานที่อาจถูกลืม</strong> — {html.escape(data['project'])} "
            f"({data['prefix']}) วันที่ {data['date'].strftime('%d/%m/%Y')}</p>"
            "<p>✓ ไม่พบงานที่ถูกลืม 🎉</p>"
        )

    items_html = "".join(
        "<li>"
        f"<strong>{html.escape(f['ref'])}</strong> "
        f'<a href="{html.escape(f["link"])}">{html.escape(f["name"])}</a><br>'
        f'<span style="font-size:12px;color:#666">สถานะ: {html.escape(f["state"])} · '
        f"ผู้รับผิดชอบ: {html.escape(f['who'])}</span><br>"
        + "<br>".join(html.escape(r) for r in f["reasons"])
        + "</li>"
        for f in flags
    )

    # สรุปตามคน
    people: dict[str, list[str]] = {}
    for f in flags:
        people.setdefault(f["who"], []).append(f"{f['ref']} {f['name'][:40]}")
    summary = "".join(
        f"<li>{html.escape(name)}: {len(items)} รายการ</li>" for name, items in people.items()
    )

    return (
        f"<p><strong>📋 ตรวจงานที่อาจถูกลืม</strong> — {html.escape(data['project'])} "
        f"({data['prefix']}) วันที่ {data['date'].strftime('%d/%m/%Y')}</p>"
        f"<p><strong>สรุปตามคน:</strong></p><ul>{summary}</ul>"
        f"<p><strong>รายละเอียด:</strong></p><ul>{items_html}</ul>"
        f'<p style="font-size:12px;color:#888">กฎ: ไม่ขยับเกิน {STALE_DAYS} วัน · '
        f"ค้างสถานะเกิน {AGING_DAYS} วัน · งาน Backlog ไม่เตือน</p>"
    )


def run_daily_report() -> bool:
    """รวมทุกขั้น: เก็บข้อมูล → render → โพสต์เข้า HUB_ISSUE_ID"""
    if not (PLANE_URL and PLANE_API_KEY and PLANE_PROJECT_ID):
        log.warning("daily report: ขาด PLANE_URL/PLANE_API_KEY/PLANE_PROJECT_ID")
        return False
    try:
        data = collect()
        body = render_html(data)
        _api(
            f"workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/issues/{HUB_ISSUE_ID}/comments/",
            payload={"comment_html": body},
            method="POST",
        )
        log.info("daily report: โพสต์แล้ว (%d รายการที่ต้องดู)", len(data["flags"]))
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("daily report ล้มเหลว: %s", exc)
        return False


# --------------------------------------------------------------------------
# scheduling helper
# --------------------------------------------------------------------------


def parse_report_time() -> tuple[int, int]:
    try:
        hh, mm = REPORT_AT.split(":")
        return int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return 8, 30


def seconds_until_next_run(now: datetime | None = None) -> float:
    """วินาทีจนถึงรอบถัดไป (ตามเวลาไทย)"""
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    hh, mm = parse_report_time()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
