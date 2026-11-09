"""
Poll Plane แล้วให้ AI สรุป issue ที่ยังไม่มีสรุป — ไม่ต้องมี webhook/public URL

รันโดย GitHub Actions (cron ทุก 15 นาที) หรือรันเองก็ได้

 uso:
    python scripts/poll_plane.py            # ทำงานจริง
    python scripts/poll_plane.py --dry-run  # แค่บอกว่าจะทำอะไร
    python scripts/poll_plane.py --limit 2  # จำกัดจำนวนต่อรอบ (default 3)
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "service"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import main as svc  # noqa: E402  (ใช้ helper เดียวกับ service)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    if not svc.PLANE_URL or not svc.PLANE_API_KEY or not svc.PLANE_PROJECT_ID:
        print("[x] ขาด PLANE_URL / PLANE_API_KEY / PLANE_PROJECT_ID")
        return 2

    project_id = svc.PLANE_PROJECT_ID
    items = svc.plane_api(
        f"workspaces/{svc.PLANE_WORKSPACE}/projects/{project_id}/work-items/?per_page=100"
    )["results"]
    states = {
        s["id"]: s
        for s in svc.plane_api(
            f"workspaces/{svc.PLANE_WORKSPACE}/projects/{project_id}/states/"
        )["results"]
    }

    def closed(it: dict) -> bool:
        st = states.get(it.get("state")) or {}
        return (st.get("group") or "").lower() in ("completed", "cancelled")

    candidates = [
        it
        for it in items
        if not it.get("archived_at")
        and not closed(it)
        and it.get("id") != svc.env("HUB_ISSUE_ID")
        and not str(it.get("name", "")).startswith("📊")
    ]

    print(f"issue ที่ยังเปิดอยู่: {len(candidates)}")

    done = 0
    for it in candidates:
        if done >= args.limit:
            print(f"ถึงขีดจำกัด {args.limit} ต่อรอบ — หยุด")
            break
        if svc.already_answered(it["id"], project_id):
            continue
        name = it.get("name", "?")[:60]
        if args.dry_run:
            print(f"  [dry] จะสรุป: THZI-{it.get('sequence_id')} {name}")
            done += 1
            continue
        try:
            issue = svc.fetch_issue(it["id"], project_id)
            result = svc.triage_issue(issue)
            svc.post_comment(it["id"], project_id, svc.render_triage_comment(result, issue))
            print(f"  ✓ สรุปแล้ว: THZI-{it.get('sequence_id')} {name}")
            done += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  x ล้มเหลว THZI-{it.get('sequence_id')}: {exc}")

    print(f"เสร็จ: {done} รายการ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
