"""
ตรวจ "งานที่อาจถูกลืม" ใน Plane — ใช้ API ล้วน ไม่ต้องใช้ AI/เครดิต

 uso:
    python scripts\\health_check.py
    python scripts\\health_check.py --stale-days 3 --aging-days 10
    python scripts\\health_check.py --post <issue-id>     # โพสต์รายงานเป็นคอมเมนต์ใน Plane

กฎที่ตรวจ:
    OVERDUE      เลย target_date และยังไม่ปิด
    NO_ASSIGNEE  งานที่เริ่มแล้ว/จะเริ่ม แต่ไม่มีเจ้าภาพ
    NO_DUE       งาน In Progress แต่ไม่มีกำหนดเสร็จ
    STALE        ไม่มีการอัปเดตเกิน N วัน (default 5) และยังไม่ปิด
    AGING        In Progress นานเกิน N วัน (default 14) นับจาก start/created
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RED, YELLOW, GREEN, DIM, RESET = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"


def load_dotenv(path: str) -> dict[str, str]:
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


ENV = load_dotenv(os.path.join(ROOT, ".env"))
BASE = (os.environ.get("PLANE_URL") or ENV.get("PLANE_URL", "")).rstrip("/")
WS = os.environ.get("PLANE_WORKSPACE") or ENV.get("PLANE_WORKSPACE", "thzi-gtm")
PID = os.environ.get("PLANE_PROJECT_ID") or ENV.get("PLANE_PROJECT_ID", "")
KEY = os.environ.get("PLANE_API_KEY") or ENV.get("PLANE_API_KEY", "")

API = f"{BASE}/api/v1"
H = {
    "X-API-Key": KEY,
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "dsh-health-check/1.0",
}


def get(path: str):
    url = f"{API}/{path}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=45) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def post(path: str, payload: dict):
    url = f"{API}/{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=H, method="POST"
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def get_project() -> dict:
    """ดึงข้อมูลโปรเจกต์ — ถ้า endpoint เดี่ยวล่ม (502) ให้ fallback ไปที่ list"""
    for attempt in range(2):
        try:
            return get(f"workspaces/{WS}/projects/{PID}/")
        except urllib.error.HTTPError as exc:
            if exc.code not in (502, 503, 504) or attempt == 1:
                break
    rows = get(f"workspaces/{WS}/projects/").get("results", [])
    for p in rows:
        if p.get("id") == PID:
            return p
    return {"name": PID, "identifier": ""}


def parse_date(value) -> date | None:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def days_since(value) -> int | None:
    d = parse_date(value)
    return None if d is None else (date.today() - d).days


def main() -> int:
    ap = argparse.ArgumentParser(description="ตรวจงานที่อาจถูกลืมใน Plane")
    ap.add_argument("--stale-days", type=int, default=5, help="ไม่ขยับเกินกี่วัน = ค้าง")
    ap.add_argument("--aging-days", type=int, default=14, help="In Progress นานเกินกี่วัน")
    ap.add_argument("--post", metavar="ISSUE_ID", help="โพสต์รายงานเป็นคอมเมนต์ใน issue นี้")
    ap.add_argument("--json", action="store_true", help="พิมพ์ JSON แทนตาราง")
    args = ap.parse_args()

    if not KEY or not BASE or not PID:
        print("ไม่พบ PLANE_API_KEY / PLANE_URL / PLANE_PROJECT_ID ใน .env")
        return 2

    project = get_project()
    states = {
        s["id"]: s for s in get(f"workspaces/{WS}/projects/{PID}/states/")["results"]
    }
    labels = {
        l["id"]: l["name"] for l in get(f"workspaces/{WS}/projects/{PID}/labels/")["results"]
    }
    members_raw = get(f"workspaces/{WS}/members/")
    members = {
        m["id"]: (m.get("first_name") or m.get("display_name") or "?")
        for m in (members_raw if isinstance(members_raw, list) else members_raw.get("results", []))
    }
    items = get(f"workspaces/{WS}/projects/{PID}/work-items/")["results"]

    prefix = project.get("identifier") or "ITEM"

    def ref(it: dict) -> str:
        return f"{prefix}-{it.get('sequence_id', '?')}"

    def link(it: dict) -> str:
        return f"{BASE}/{WS}/projects/{PID}/issues/{it['id']}/"

    def state_group(it: dict) -> str:
        st = states.get(it.get("state")) or {}
        return (st.get("group") or "").lower()

    def state_name(it: dict) -> str:
        st = states.get(it.get("state")) or {}
        return st.get("name") or "?"

    def who(it: dict) -> str:
        ids = it.get("assignees") or []
        return ", ".join(members.get(i, str(i)[:8]) for i in ids) or "— ไม่มีเจ้าภาพ —"

    flags: list[dict] = []
    today = date.today()

    for it in items:
        if it.get("archived_at"):
            continue
        grp = state_group(it)
        closed = grp in ("completed", "cancelled")
        reasons: list[str] = []
        severity = "info"

        target = parse_date(it.get("target_date"))
        if not closed and target and target < today:
            reasons.append(f"เลยกำหนด {target.strftime('%d/%m/%Y')} มา {(today - target).days} วัน")
            severity = "high"

        n_assignees = len(it.get("assignees") or [])
        if not closed and n_assignees == 0 and grp != "backlog":
            reasons.append("ไม่มีผู้รับผิดชอบ")
            severity = "high"

        if not closed and grp == "started" and not target:
            reasons.append("กำลังทำแต่ไม่มีกำหนดเสร็จ")
            severity = max(severity, "med", key=["info", "med", "high"].index)

        upd_days = days_since(it.get("updated_at"))
        if not closed and upd_days is not None and upd_days > args.stale_days:
            reasons.append(f"ไม่ขยับมา {upd_days} วัน")
            severity = "high" if upd_days > args.stale_days * 2 else max(severity, "med", key=["info", "med", "high"].index)

        age_src = it.get("start_date") or it.get("created_at")
        age_days = days_since(age_src)
        if not closed and grp == "started" and age_days is not None and age_days > args.aging_days:
            reasons.append(f"ค้างสถานะนี้ {age_days} วัน")
            severity = max(severity, "med", key=["info", "med", "high"].index)

        if reasons:
            flags.append(
                {
                    "ref": ref(it),
                    "name": it.get("name", "?"),
                    "state": state_name(it),
                    "who": who(it),
                    "assignee_ids": it.get("assignees") or [],
                    "reasons": reasons,
                    "severity": severity,
                    "link": link(it),
                    "labels": [labels.get(l, l) for l in (it.get("labels") or [])],
                }
            )

    order = {"high": 0, "med": 1, "info": 2}
    flags.sort(key=lambda f: (order[f["severity"]], f["ref"]))

    if args.json:
        print(json.dumps({"project": project.get("name"), "date": str(today), "flags": flags},
                         ensure_ascii=False, indent=2))
        return 0

    icon = {"high": f"{RED}🔴{RESET}", "med": f"{YELLOW}🟡{RESET}", "info": f"{DIM}⚪{RESET}"}
    print(f"\n{GREEN}📋 ตรวจงานที่อาจถูกลืม{RESET} — {project.get('name')} ({prefix})  วันที่ {today.strftime('%d/%m/%Y')}")
    print(f"{DIM}กฎ: stale>{args.stale_days}วัน, aging>{args.aging_days}วัน | งานที่ต้องปิด {len(items)} รายการ{RESET}\n")

    if not flags:
        print(f"{GREEN}✓ ไม่พบงานที่น่าสงสัย{RESET}")
        return 0

    for f in flags:
        print(f"{icon[f['severity']]} {f['ref']:<9} {f['name'][:60]}")
        print(f"    {DIM}สถานะ: {f['state']} | รับผิดชอบ: {f['who']}{RESET}")
        for r in f["reasons"]:
            print(f"    → {r}")
        print()

    # สรุปตามคน
    print(f"{DIM}{'-' * 60}{RESET}")
    print(f"{GREEN}สรุปตามคน{RESET}")
    people: dict[str, list[dict]] = {}
    for f in flags:
        if f["assignee_ids"]:
            for a in f["assignee_ids"]:
                people.setdefault(members.get(a, str(a)[:8]), []).append(f)
        else:
            people.setdefault("(ยังไม่มีเจ้าภาพ)", []).append(f)
    for name, fs in sorted(people.items(), key=lambda kv: -len(kv[1])):
        hi = sum(1 for x in fs if x["severity"] == "high")
        print(f"  • {name}: {len(fs)} รายการ" + (f" {RED}(ด่วน {hi}){RESET}" if hi else ""))
        for x in fs:
            print(f"      - {x['ref']} {x['name'][:44]}")

    if args.post:
        body = render_html(project, prefix, flags, args, today)
        post(
            f"workspaces/{WS}/projects/{PID}/issues/{args.post}/comments/",
            {"comment_html": body},
        )
        print(f"\n{GREEN}โพสต์รายงานเป็นคอมเมนต์ใน {args.post} แล้ว{RESET}")

    return 1 if any(f["severity"] == "high" for f in flags) else 0


def render_html(project, prefix, flags, args, today) -> str:
    rows = []
    for f in flags:
        reasons = "<br>".join(html.escape(r) for r in f["reasons"])
        rows.append(
            f"<li><strong>{html.escape(f['ref'])}</strong> "
            f'<a href="{html.escape(f["link"])}">{html.escape(f["name"])}</a><br>'
            f'<span style="font-size:12px;color:#666">สถานะ: {html.escape(f["state"])} · '
            f'ผู้รับผิดชอบ: {html.escape(f["who"])}</span><br>{reasons}</li>'
        )
    return (
        f"<p><strong>📋 ตรวจงานที่อาจถูกลืม</strong> — {html.escape(project.get('name',''))} "
        f"({prefix}) วันที่ {today.strftime('%d/%m/%Y')}</p>"
        f'<ul>{"".join(rows)}</ul>'
        f'<p style="font-size:12px;color:#888">กฎ: ไม่ขยับเกิน {args.stale_days} วัน / '
        f"In Progress นานเกิน {args.aging_days} วัน</p>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
