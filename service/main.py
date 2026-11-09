"""
Plane ↔ DeepSeek webhook service
================================

รับ webhook จาก Plane (self-hosted) แล้วให้ DeepSeek:
  • สรุป issue ที่เพิ่งถูกสร้าง/แก้ไข
  • เสนอ priority + label
  • เขียนกลับเป็น comment ใน issue นั้น

ใช้ standard library ล้วน (ไม่ต้อง pip install) → Docker image เล็กและเสถียร

Environment variables (ตั้งใน Coolify):
  PLANE_URL               https://plane.example.com
  PLANE_WORKSPACE         thzi-gtm
  PLANE_API_KEY           token ของ Plane
  PLANE_WEBHOOK_SECRET    secret ที่ตั้งตอนสร้าง webhook ใน Plane (ถ้าไม่มี = ไม่ตรวจ signature)
  PLANE_PROJECT_ID        (fallback) project id ถ้า payload ไม่มีมา
  DEEPSEEK_API_KEY        sk-...
  DEEPSEEK_BASE_URL       default https://api.deepseek.com
  DEEPSEEK_MODEL          default deepseek-chat
  BOT_USER_ID             id ของ user ที่ token นี้ใช้ (กันตอบคอมเมนต์ตัวเอง)
  PORT                    default 8080
  DRY_RUN                 1 = ไม่ยิง API จริง แค่ log (ใช้ทดสอบ)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows console เข้ารหัสไทยไม่ได้ — กัน log เพี้ยน/crash
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, val = line.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key.strip()] = val
    return values


_FILE_ENV = load_dotenv(os.path.join(ROOT, ".env"))
_FILE_ENV.update(load_dotenv(os.path.join(os.path.dirname(ROOT), ".env")))


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or _FILE_ENV.get(name) or default).strip()


PLANE_URL = env("PLANE_URL").rstrip("/")
PLANE_WORKSPACE = env("PLANE_WORKSPACE", "thzi-gtm")
PLANE_API_KEY = env("PLANE_API_KEY")
PLANE_WEBHOOK_SECRET = env("PLANE_WEBHOOK_SECRET")
PLANE_PROJECT_ID = env("PLANE_PROJECT_ID")

# ---- AI provider (OpenAI-compatible) ----
# ใช้ AI_PROVIDER เพื่อเลือก preset แล้ว override ด้วย AI_BASE_URL / AI_MODEL / AI_API_KEY
# (ชื่อ DEEPSEEK_* เดิมยังใช้ได้ เพื่อความเข้ากันได้กับ .env ที่มีอยู่)
AI_PRESETS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
    ),
    "openrouter": ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3.1:free"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    # ฟรี ไม่ต้องสมัคร ไม่ต้องใช้ key
    "pollinations": ("https://text.pollinations.ai/openai", "openai"),
}
AI_PROVIDER = (env("AI_PROVIDER", "deepseek") or "deepseek").lower()
_preset_base, _preset_model = AI_PRESETS.get(AI_PROVIDER, AI_PRESETS["deepseek"])

# ค่า DEEPSEEK_* เดิม ใช้เฉพาะเมื่อ provider เป็น deepseek (กันการชนกัน)
if AI_PROVIDER == "deepseek":
    _legacy_key = env("DEEPSEEK_API_KEY")
    _legacy_base = env("DEEPSEEK_BASE_URL")
    _legacy_model = env("DEEPSEEK_MODEL")
else:
    _legacy_key = _legacy_base = _legacy_model = ""

DEEPSEEK_API_KEY = env("AI_API_KEY") or _legacy_key
DEEPSEEK_BASE_URL = (env("AI_BASE_URL") or _legacy_base or _preset_base).rstrip("/")
DEEPSEEK_MODEL = env("AI_MODEL") or _legacy_model or _preset_model
BOT_USER_ID = env("BOT_USER_ID")
PORT = int(env("PORT", "8080") or 8080)
DRY_RUN = env("DRY_RUN", "0") in ("1", "true", "yes")
DAILY_REPORT = env("DAILY_REPORT", "1") in ("1", "true", "yes")
# ตอบคอมเมนต์ด้วย AI ไหม (ปิดได้เพื่อประหยัดโควตา free tier)
AI_REPLY_ON_COMMENT = env("AI_REPLY_ON_COMMENT", "1") in ("1", "true", "yes")
# เว้นระยะระหว่างการตอบใน issue เดียวกัน (วินาที) — กันโควตา free tier หมด
AI_REPLY_COOLDOWN = int(env("AI_REPLY_COOLDOWN", "90") or 90)

try:
    import report as daily_report  # service/report.py
except Exception:  # noqa: BLE001
    daily_report = None

BOT_MARKER = "🤖 DeepSeek Assistant"
# บางผู้ให้บริการ (Groq ผ่าน Cloudflare) บล็อก UA ของ urllib — ต้องระบุเอง
USER_AGENT = env("AI_USER_AGENT") or "plane-deepseek-bot/1.0 (+https://github.com/pearkanyanat/plane-deepseek-bot)"
# คอมเมนต์ที่บอทสร้างเองทั้งหมด — ใช้กัน webhook ย้อนกลับมาหาบอท
BOT_MARKERS = (BOT_MARKER, "📋 ตรวจงานที่อาจถูกลืม")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("plane-deepseek")

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def dig(obj, *path, default=None):
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def as_id(value) -> str | None:
    """รับได้ทั้ง uuid string หรือ object {id: uuid}"""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        v = value.get("id")
        return v if isinstance(v, str) and v else None
    return None


def http_json(method: str, url: str, api_key: str, payload=None, timeout=60) -> dict:
    data = None
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "plane-deepseek-bot/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def plane_api(path: str, payload=None, method: str = "GET") -> dict:
    url = f"{PLANE_URL}/api/v1/{path.lstrip('/')}"
    return http_json(method, url, PLANE_API_KEY, payload)


def deepseek(messages: list[dict], timeout=120, retries: int = 2) -> str:
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 900,
        "stream": False,
    }
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"

    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY or 'anonymous'}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            # free tier บางเจ้าจำกัดความถี่ (402/429) — รอแล้วลองใหม่
            if exc.code in (402, 429) and attempt < retries and (
                "FREE_LIMIT" in body or "limit" in body.lower()
            ):
                m = re.search(r"Retry after (\d+)", body)
                wait = min(int(m.group(1)) + 3, 60) if m else 20 + attempt * 20
                log.warning("rate limit (%s) — รอ %ss แล้วลองใหม่", exc.code, wait)
                time.sleep(wait)
                continue
            raise
    else:
        raise RuntimeError("DeepSeek/ผู้ให้บริการ AI ไม่ตอบกลับ")

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"AI ตอบผิดรูป: {str(data)[:300]}")
    return (choices[0].get("message") or {}).get("content") or ""


def html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def html_to_text(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .strip()
    )


# --------------------------------------------------------------------------
# Plane calls
# --------------------------------------------------------------------------


def fetch_issue(issue_id: str, project_id: str) -> dict:
    return plane_api(
        f"workspaces/{PLANE_WORKSPACE}/projects/{project_id}/issues/{issue_id}/"
    )


def fetch_comments(issue_id: str, project_id: str) -> list[dict]:
    data = plane_api(
        f"workspaces/{PLANE_WORKSPACE}/projects/{project_id}/issues/{issue_id}/comments/"
    )
    return data.get("results") or []


def post_comment(issue_id: str, project_id: str, body_html: str) -> dict:
    return plane_api(
        f"workspaces/{PLANE_WORKSPACE}/projects/{project_id}/issues/{issue_id}/comments/",
        payload={"comment_html": body_html},
        method="POST",
    )


def already_answered(issue_id: str, project_id: str) -> bool:
    """กันตอบซ้ำ — ถ้าบอทเคยคอมเมนต์ใน issue นี้แล้ว ไม่ทำอีก"""
    try:
        for c in fetch_comments(issue_id, project_id):
            if BOT_MARKER in (c.get("comment_stripped") or c.get("comment_html") or ""):
                return True
    except Exception as exc:  # noqa: BLE001
        log.warning("เช็กคอมเมนต์เดิมไม่ได้ (%s) — จะตอบต่อ", exc)
    return False


# --------------------------------------------------------------------------
# AI
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "คุณเป็นผู้ช่วยจัดการโปรเจกต์ซอฟต์แวร์ชื่อ 'DeepSeek Assistant' "
    "ตอบเป็นภาษาไทย กระชับ ตรงประเด็น ไม่พูดน้ำเยอะ\n"
    "ตอบเป็น JSON เท่านั้น ตาม schema:\n"
    '{"summary": "สรุปงาน 1-2 ประโยค", '
    '"priority": "urgent|high|medium|low", '
    '"labels": ["label1","label2"], '
    '"next_steps": ["ขั้นตอนถัดไป 1","ขั้นตอนถัดไป 2"]}'
)


def customer_context(label_names: list[str]) -> str:
    """ดึงข้อมูลลูกค้าจาก CUSTOMERS.md ตาม label cust:<slug>"""
    slugs = [n.split(":", 1)[1] for n in label_names if n.startswith("cust:")]
    if not slugs:
        return ""
    for path in (
        os.path.join(os.path.dirname(ROOT), "CUSTOMERS.md"),
        os.path.join(ROOT, "CUSTOMERS.md"),
    ):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        chunks = []
        for slug in slugs:
            marker = f"cust:{slug}"
            idx = text.find(marker)
            if idx == -1:
                continue
            start = text.rfind("\n### ", 0, idx)
            start = start if start != -1 else idx
            end = text.find("\n### ", idx)
            end = end if end != -1 else min(len(text), idx + 900)
            chunks.append(text[start:end].strip()[:900])
        if chunks:
            return "\n\n".join(chunks)
    return ""


def build_prompt(issue: dict, extra: str = "") -> str:
    title = issue.get("name") or "(ไม่มีชื่อ)"
    desc = html_to_text(issue.get("description_stripped") or issue.get("description_html") or "")
    state = dig(issue, "state", "name") or issue.get("state") or "?"
    priority = issue.get("priority") or "none"
    assignees = issue.get("assignees") or []
    base = (
        f"ชื่อ issue: {title}\n"
        f"สถานะ: {state} | priority ปัจจุบัน: {priority}\n"
        f"ผู้รับผิดชอบ: {len(assignees)} คน\n"
        f"รายละเอียด:\n{desc[:4000] or '(ว่าง)'}\n"
    )
    if extra:
        base += f"\nข้อมูลลูกค้าที่เกี่ยวข้อง (ใช้ประกอบการวิเคราะห์):\n{extra}\n"
    return base + "\nช่วยสรุป สร้าง label ที่เหมาะสม และเสนอขั้นตอนถัดไป"


def parse_ai_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"ไม่พบ JSON ในคำตอบ: {raw[:200]}")
    return json.loads(text[start : end + 1])


def triage_issue(issue: dict, extra: str = "") -> dict:
    raw = deepseek(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(issue, extra)},
        ]
    )
    return parse_ai_json(raw)


# --------------------------------------------------------------------------
# webhook handling
# --------------------------------------------------------------------------


class Deduper:
    """กันประมวลผลเหตุการณ์ซ้ำ/ถี่เกินไป"""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_time(self, key: str, cooldown: float = 30.0) -> bool:
        now = time.time()
        with self._lock:
            last = self._seen.get(key)
            if last is not None and now - last < cooldown:
                return False
            self._seen[key] = now
            if len(self._seen) > 5000:
                cutoff = now - 86400
                self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        return True


DEDUPE = Deduper()


def verify_signature(body: bytes, signature: str | None) -> bool:
    if not PLANE_WEBHOOK_SECRET:
        return True  # ไม่ได้ตั้ง secret = ไม่ตรวจ
    if not signature:
        return False
    expected = hmac.new(
        PLANE_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def handle_event(payload: dict) -> None:
    event = payload.get("event", "")
    action = payload.get("action", "")
    data = payload.get("data") or {}
    actor = as_id(payload.get("actor")) or as_id(data.get("actor_id"))

    log.info("event=%s action=%s actor=%s", event, action, actor)

    # กันตอบตัวเอง (loop)
    if BOT_USER_ID and actor == BOT_USER_ID:
        log.info("ข้าม: ผู้กระทำคือบอทเอง")
        return

    # ---- หา issue id + project id จาก payload (รองรับหลายรูปแบบ) ----
    if event == "issue":
        issue_id = as_id(data.get("id")) or as_id(data.get("issue"))
    elif event in ("issue_comment", "comment"):
        issue_id = as_id(data.get("issue")) or as_id(payload.get("issue"))
    else:
        log.info("ข้าม event ชนิดนี้")
        return

    project_id = (
        as_id(data.get("project"))
        or as_id(data.get("project_id"))
        or as_id(payload.get("project"))
        or PLANE_PROJECT_ID
    )

    if not issue_id or not project_id:
        log.warning("payload ไม่มี issue/project id — ข้าม: %s", json.dumps(payload)[:400])
        return

    # key ต้องรวม id ของ event ด้วย ไม่งั้นคอมเมนต์คนละอันใน issue เดียวกันจะถูกข้าม
    event_key = f"{event}:{issue_id}:{action}:{as_id(data.get('id')) or ''}"
    if not DEDUPE.first_time(event_key):
        log.info("ข้าม: ทำเรื่องนี้ไปเมื่อสักครู่แล้ว")
        return

    if event == "issue_comment":
        if not AI_REPLY_ON_COMMENT:
            log.info("ข้าม: ปิดการตอบคอมเมนต์ (AI_REPLY_ON_COMMENT=0)")
            return
        if not DEDUPE.first_time(f"reply:{issue_id}", cooldown=float(AI_REPLY_COOLDOWN)):
            log.info("ข้าม: เพิ่งตอบ issue นี้ไป — เว้นระยะ %ss", AI_REPLY_COOLDOWN)
            return
        handle_comment_reply(issue_id, project_id, data)
        return

    # ---- triage ----
    if DRY_RUN:
        log.info("[DRY_RUN] จะสรุป issue %s (project %s)", issue_id, project_id)
        return

    if already_answered(issue_id, project_id):
        log.info("ข้าม: issue นี้มีคำตอบของบอทแล้ว")
        return

    issue = fetch_issue(issue_id, project_id)
    try:
        label_names = {
            l["id"]: l["name"]
            for l in plane_api(
                f"workspaces/{PLANE_WORKSPACE}/projects/{project_id}/labels/"
            )["results"]
        }
        names = [label_names.get(x, "") for x in (issue.get("labels") or [])]
        extra = customer_context(names)
    except Exception:  # noqa: BLE001
        extra = ""
    result = triage_issue(issue, extra)
    body = render_triage_comment(result, issue)
    post_comment(issue_id, project_id, body)
    log.info("โพสต์สรุปให้ issue %s เรียบร้อย", issue_id)


def handle_comment_reply(issue_id: str, project_id: str, data: dict) -> None:
    comment_html = data.get("comment_html") or data.get("comment_stripped") or ""
    if any(m in comment_html for m in BOT_MARKERS):
        log.info("ข้าม: คอมเมนต์นั้นเป็นของบอทเอง")
        return
    if BOT_USER_ID and (as_id(data.get("actor_id")) or as_id(data.get("created_by"))) == BOT_USER_ID:
        log.info("ข้าม: ผู้เขียนคอมเมนต์คือบอทเอง")
        return

    question = html_to_text(comment_html)
    if not question.strip():
        return

    log.info("มีคอมเมนต์ใหม่ใน issue %s — กำลังตอบ", issue_id)
    if DRY_RUN:
        log.info("[DRY_RUN] จะตอบคอมเมนต์: %s", question[:120])
        return

    issue = fetch_issue(issue_id, project_id)
    answer = deepseek(
        [
            {
                "role": "system",
                "content": (
                    "คุณเป็นผู้ช่วยในทีม พยายามตอบคำถามเกี่ยวกับ issue นี้ให้ตรงจุด "
                    "ตอบภาษาไทย ไม่เกิน 6-8 บรรทัด "
                    "ห้ามใช้ markdown, ดอกจัน (**), หรือหัวข้อ — เขียนเป็นประโยคธรรมดาเท่านั้น "
                    "ถ้าไม่มั่นใจให้บอกว่าต้องให้คนตรวจสอบ"
                ),
            },
            {"role": "user", "content": f"{build_prompt(issue)}\n\nคำถาม/คอมเมนต์:\n{question}"},
        ]
    )
    body = (
        f"<p>{BOT_MARKER} ตอบกลับ:</p>"
        f"<p>{html_escape(answer).replace(chr(10), '<br>')}</p>"
    )
    post_comment(issue_id, project_id, body)
    log.info("ตอบคอมเมนต์ issue %s แล้ว", issue_id)


def render_triage_comment(result: dict, issue: dict) -> str:
    summary = html_escape(result.get("summary", "-"))
    priority = html_escape(result.get("priority", "-"))
    labels = result.get("labels") or []
    steps = result.get("next_steps") or []

    labels_html = "".join(
        f'<span style="background:#eef2ff;padding:1px 6px;border-radius:4px;'
        f'font-size:12px;margin-right:4px;">{html_escape(str(l))}</span>'
        for l in labels[:5]
    )
    steps_html = "".join(f"<li>{html_escape(str(s))}</li>" for s in steps[:5]) or "<li>-</li>"

    return (
        f"<p><strong>{BOT_MARKER}</strong></p>"
        f"<p><strong>สรุป:</strong> {summary}</p>"
        f"<p><strong>Priority ที่แนะนำ:</strong> {priority}</p>"
        f"<p><strong>Label:</strong> {labels_html or '-'}</p>"
        f"<p><strong>ขั้นตอนถัดไป:</strong></p><ul>{steps_html}</ul>"
    )


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "plane-deepseek/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # ใช้ logger ของเราเอง
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/healthz", "/health", ""):
            self._send(
                200,
                {
                    "ok": True,
                    "dry_run": DRY_RUN,
                    "workspace": PLANE_WORKSPACE,
                    "plane_configured": bool(PLANE_URL and PLANE_API_KEY),
                    "deepseek_configured": bool(DEEPSEEK_API_KEY),
                    "daily_report": bool(DAILY_REPORT and daily_report),
                    "daily_report_at": (daily_report.REPORT_AT if daily_report else None),
                },
            )
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/webhook/plane"):
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        signature = (
            self.headers.get("X-Plane-Signature")
            or self.headers.get("X-Hub-Signature-256")
        )
        if not verify_signature(body, signature):
            log.warning("signature ไม่ถูกต้อง — ปฏิเสธ")
            self._send(401, {"error": "invalid signature"})
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, {"error": "invalid json"})
            return

        # ตอบ 200 ทันที แล้วทำงานเบื้องหลัง (Plane ไม่ต้องรอ AI)
        self._send(200, {"received": True})
        threading.Thread(target=safe_handle, args=(payload,), daemon=True).start()

    def do_HEAD(self):  # noqa: N802
        self._send(200, {})


def safe_handle(payload: dict) -> None:
    try:
        handle_event(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        log.error("HTTP %s จาก API: %s", exc.code, detail)
    except Exception as exc:  # noqa: BLE001
        log.exception("ประมวลผล webhook ล้มเหลว: %s", exc)


# --------------------------------------------------------------------------
# daily report thread
# --------------------------------------------------------------------------


def daily_report_loop() -> None:
    """รอจนถึงเวลาที่ตั้งไว้ แล้วโพสต์รายงาน — วนซ้ำทุกวัน"""
    if daily_report is None:
        return
    wait = daily_report.seconds_until_next_run()
    log.info(
        "daily report เปิดใช้ — รอบถัดไป %s (อีก %.0f นาที)",
        daily_report.REPORT_AT,
        wait / 60,
    )
    while True:
        time.sleep(wait)
        try:
            daily_report.run_daily_report()
        except Exception as exc:  # noqa: BLE001
            log.exception("daily report ล้มเหลว: %s", exc)
        wait = daily_report.seconds_until_next_run()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> None:
    missing = []
    if not PLANE_URL:
        missing.append("PLANE_URL")
    if not PLANE_API_KEY:
        missing.append("PLANE_API_KEY")
    if not DEEPSEEK_API_KEY and AI_PROVIDER not in ("ollama", "pollinations"):
        missing.append("AI_API_KEY")

    log.info(
        "Port %s | workspace=%s | dry_run=%s | AI=%s (%s)",
        PORT,
        PLANE_WORKSPACE,
        DRY_RUN,
        AI_PROVIDER,
        DEEPSEEK_MODEL,
    )
    if missing:
        log.warning("ยังขาด env: %s (จะรันได้แต่จะทำงานไม่ครบ)", ", ".join(missing))
    if not PLANE_WEBHOOK_SECRET:
        log.warning(
            "ไม่ได้ตั้ง PLANE_WEBHOOK_SECRET — จะไม่ตรวจ signature "
            "(มีคนยิง webhook ปลอมได้)"
        )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("พร้อมรับ webhook ที่ POST /webhook/plane")

    if DAILY_REPORT:
        if daily_report is None:
            log.warning("ไม่พบ service/report.py — ปิด daily report")
        else:
            threading.Thread(
                target=daily_report_loop, daemon=True, name="daily-report"
            ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("ปิด server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
