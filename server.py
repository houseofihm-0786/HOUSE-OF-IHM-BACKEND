from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import secrets
import asyncio
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List
import uuid
from datetime import datetime, timezone, timedelta

import resend
import requests


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Logging — initialise early so route handlers can use it
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Resend setup (gracefully no-ops if API key missing)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev").strip()
AUTOREPLY_FROM_EMAIL = os.environ.get("AUTOREPLY_FROM_EMAIL", "").strip() or RESEND_FROM_EMAIL
CONTACT_RECIPIENT_EMAIL = os.environ.get("CONTACT_RECIPIENT_EMAIL", "").strip()
REPLY_TO_EMAIL = os.environ.get("REPLY_TO_EMAIL", "").strip()
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# Telegram setup (gracefully no-ops if token/chat-id missing)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# Public backend URL used for webhook registration (optional — if empty, webhook auto-setup skipped)
BACKEND_PUBLIC_URL = os.environ.get("BACKEND_PUBLIC_URL", "").strip().rstrip("/")
# Secret token Telegram must pass in the X-Telegram-Bot-Api-Secret-Token header
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
if TELEGRAM_BOT_TOKEN and not TELEGRAM_WEBHOOK_SECRET:
    TELEGRAM_WEBHOOK_SECRET = secrets.token_hex(24)

# Anti-spam: block duplicate submissions from same email within this many seconds
DUPLICATE_WINDOW_SECONDS = 30

# Admin auth — simple HTTP Basic against env vars
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
_http_basic = HTTPBasic(auto_error=False)


def require_admin(creds: HTTPBasicCredentials = Depends(_http_basic)):
    """Dependency that enforces valid admin Basic credentials."""
    if not (ADMIN_USERNAME and ADMIN_PASSWORD):
        raise HTTPException(status_code=503, detail="Admin auth not configured")
    if (
        not creds
        or not secrets.compare_digest(creds.username or "", ADMIN_USERNAME)
        or not secrets.compare_digest(creds.password or "", ADMIN_PASSWORD)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# Create the main app without a prefix
app = FastAPI(title="House of IHM EdTech API")
api_router = APIRouter(prefix="/api")


# ---------- Models ----------
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class ContactCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    message: str = Field(..., min_length=1, max_length=5000)


class Contact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    email: EmailStr
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notification_status: str = "pending"  # pending | delivered | failed | skipped
    autoreply_status: str = "pending"
    telegram_status: str = "pending"


async def _update_contact_status(contact_id: str, **fields) -> None:
    try:
        await db.contacts.update_one({"id": contact_id}, {"$set": fields})
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to update contact status {contact_id}: {e}")


# ---------- Helpers ----------
def _build_contact_email_html(contact: Contact) -> str:
    safe_msg = (contact.message or "").replace("\n", "<br>")
    ts = contact.created_at.strftime("%d %b %Y · %H:%M UTC")
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f5f5f7;padding:32px 0;">
      <tr><td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;border:1px solid #e5e5ea;padding:36px;">
          <tr><td>
            <p style="margin:0;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:#86868b;">House of IHM · New enquiry</p>
            <h1 style="margin:14px 0 0;font-size:22px;color:#1d1d1f;letter-spacing:-0.02em;">[Enquiry] {contact.name}</h1>

            <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;border-collapse:collapse;">
              <tr>
                <td width="120" style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;color:#86868b;vertical-align:top;">Name</td>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:15px;color:#1d1d1f;">{contact.name}</td>
              </tr>
              <tr>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;color:#86868b;vertical-align:top;">Email</td>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:15px;color:#1d1d1f;"><a href="mailto:{contact.email}" style="color:#2997ff;text-decoration:none;font-weight:500;">{contact.email}</a></td>
              </tr>
              <tr>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;color:#86868b;vertical-align:top;">Message</td>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;font-size:15px;line-height:1.65;color:#1d1d1f;">{safe_msg}</td>
              </tr>
              <tr>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;border-bottom:1px solid #e5e5ea;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;color:#86868b;vertical-align:top;">Timestamp</td>
                <td style="padding:14px 0;border-top:1px solid #e5e5ea;border-bottom:1px solid #e5e5ea;font-size:13px;color:#6e6e73;font-family:monospace;">{ts}</td>
              </tr>
            </table>

            <p style="margin:28px 0 0;font-size:12px;color:#86868b;">
              Hit <strong>Reply</strong> to respond directly to {contact.name} — your message will go to <a href="mailto:{contact.email}" style="color:#2997ff;text-decoration:none;">{contact.email}</a>.
            </p>

            <hr style="border:none;border-top:1px solid #e5e5ea;margin:28px 0 18px;">
            <p style="margin:0;font-size:12px;color:#1d1d1f;letter-spacing:-0.01em;"><strong>House of IHM</strong></p>
            <p style="margin:4px 0 0;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:#86868b;">Where Vision Meets Execution</p>
          </td></tr>
        </table>
      </td></tr>
    </table>
    """


async def _send_contact_notification(contact: Contact) -> None:
    """Send the contact-form notification email. Failures are logged, never raised."""
    if not (RESEND_API_KEY and CONTACT_RECIPIENT_EMAIL):
        logger.info("Resend not configured — skipping email notification")
        await _update_contact_status(contact.id, notification_status="skipped")
        return
    params = {
        "from": RESEND_FROM_EMAIL,
        "to": [CONTACT_RECIPIENT_EMAIL],
        "reply_to": [contact.email],
        "subject": f"[Enquiry — {contact.name}] House of IHM",
        "html": _build_contact_email_html(contact),
        "headers": {
            "X-Mailer": "House of IHM Web Form",
            "X-Priority": "1",
            "Importance": "high",
        },
    }
    try:
        result = await asyncio.to_thread(resend.Emails.send, params)
        email_id = result.get("id")
        logger.info(f"Contact email delivered (id={email_id})")
        await _update_contact_status(
            contact.id,
            notification_status="delivered",
            notification_email_id=email_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to send contact email: {e}")
        await _update_contact_status(
            contact.id,
            notification_status="failed",
            notification_error=str(e)[:500],
        )


def _build_auto_reply_html(contact: Contact) -> str:
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0e0e10;padding:32px 0;">
      <tr><td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#1d1d1f;border-radius:20px;border:1px solid #2a2a2d;padding:40px;">
          <tr><td>
            <p style="margin:0;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:#86868b;">House of IHM EdTech</p>
            <h1 style="margin:18px 0 0;font-size:26px;color:#ffffff;letter-spacing:-0.02em;line-height:1.2;">Thank you for reaching out, {contact.name}.</h1>
            <p style="margin:22px 0 0;font-size:15px;line-height:1.65;color:#d2d2d7;">
              We've received your message and our team will get back to you shortly.
            </p>
            <p style="margin:14px 0 0;font-size:15px;line-height:1.65;color:#d2d2d7;">
              If your request is urgent, you may contact us directly at <a href="mailto:hello@houseofihm.com" style="color:#2997ff;text-decoration:none;font-weight:500;">hello@houseofihm.com</a>
            </p>
            <hr style="border:none;border-top:1px solid #2a2a2d;margin:32px 0;">
            <p style="margin:0;font-size:13px;color:#ffffff;letter-spacing:-0.01em;">— House of IHM</p>
            <p style="margin:6px 0 0;font-size:11px;letter-spacing:0.22em;text-transform:uppercase;color:#6e6e73;">
              <span style="display:inline-block;width:6px;height:6px;border-radius:9999px;background:#2997ff;vertical-align:middle;margin-right:8px;"></span>
              Part of BHM Sunrise Pvt. Ltd.
            </p>
          </td></tr>
        </table>
      </td></tr>
    </table>
    """


async def _send_auto_reply(contact: Contact) -> None:
    """Send a confirmation auto-reply to the form submitter. Failures are logged, never raised."""
    if not RESEND_API_KEY:
        logger.info("Resend not configured — skipping auto-reply")
        await _update_contact_status(contact.id, autoreply_status="skipped")
        return
    params = {
        "from": AUTOREPLY_FROM_EMAIL,
        "to": [contact.email],
        "subject": "Thank you for reaching out — House of IHM",
        "html": _build_auto_reply_html(contact),
        "headers": {"X-Mailer": "House of IHM Web Form"},
    }
    if REPLY_TO_EMAIL:
        params["reply_to"] = [REPLY_TO_EMAIL]
    try:
        result = await asyncio.to_thread(resend.Emails.send, params)
        email_id = result.get("id")
        logger.info(f"Auto-reply delivered to {contact.email} (id={email_id})")
        await _update_contact_status(
            contact.id,
            autoreply_status="delivered",
            autoreply_email_id=email_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to send auto-reply to {contact.email}: {e}")
        await _update_contact_status(
            contact.id,
            autoreply_status="failed",
            autoreply_error=str(e)[:500],
        )


def _telegram_post(token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    """Synchronous Telegram sendMessage — wrapped via asyncio.to_thread."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _detect_priority(contact: Contact) -> str | None:
    """Light keyword-based priority tag on the message body."""
    text = f"{contact.name} {contact.message}".lower()
    keyword_map = [
        ("💼 Partnership", ("partnership", "partner", "collaborate", "collaboration")),
        ("💰 Investment", ("invest", "investor", "funding", "venture capital", "vc ")),
        ("📰 Press", ("press", "journalist", "media", "interview", "feature")),
        ("🎓 Student", ("student", "learner", "enrol", "enroll", "admission")),
        ("👨‍🏫 Teacher", ("teacher", "educator", "tutor", "faculty")),
        ("🚨 Urgent", ("urgent", "asap", "immediate", "priority")),
    ]
    for tag, words in keyword_map:
        if any(w in text for w in words):
            return tag
    return None


async def _send_telegram_alert(contact: Contact) -> None:
    """Send an instant Telegram alert. Failures are logged, never raised."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        logger.info("Telegram not configured — skipping alert")
        await _update_contact_status(contact.id, telegram_status="skipped")
        return
    preview = (contact.message or "").strip().replace("\n", " ")
    if len(preview) > 100:
        preview = preview[:100].rstrip() + "…"

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    ts = contact.created_at.strftime("%d %b %Y · %H:%M UTC")
    priority_tag = _detect_priority(contact)
    header = "🚀 <b>New Enquiry — House of IHM</b>"
    if priority_tag:
        header = f"{priority_tag}  ·  " + header

    text = (
        f"{header}\n\n"
        f"<b>Name:</b> {esc(contact.name)}\n"
        f"<b>Email:</b> {esc(contact.email)}\n"
        f"<b>Message:</b> {esc(preview)}\n"
        f"<b>Time:</b> {ts}"
    )

    # Inline reply button — Gmail compose (works universally on web + mobile)
    gmail_compose_url = (
        "https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={contact.email}"
        "&su=Re:%20Your%20enquiry%20to%20House%20of%20IHM"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Mark as handled", "callback_data": f"handle:{contact.id}"},
            ],
            [
                {"text": "✉️ Reply by email", "url": gmail_compose_url},
            ],
        ]
    }

    try:
        result = await asyncio.to_thread(
            _telegram_post,
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            text,
            reply_markup,
        )
        msg_id = (result or {}).get("result", {}).get("message_id")
        logger.info(f"Telegram alert delivered (message_id={msg_id})")
        await _update_contact_status(
            contact.id,
            telegram_status="delivered",
            telegram_message_id=msg_id,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            priority_tag=priority_tag,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to send Telegram alert: {e}")
        await _update_contact_status(
            contact.id,
            telegram_status="failed",
            telegram_error=str(e)[:500],
        )


# ---------- Telegram callback + webhook ----------
def _telegram_api(method: str, payload: dict) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram bot token missing")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


async def _handle_mark_handled(contact_id: str, callback_id: str, chat_id: int, message_id: int, actor: str):
    """Mark a contact as handled and update the Telegram message to reflect it."""
    handled_at = datetime.now(timezone.utc)
    existing = await db.contacts.find_one({"id": contact_id}, {"_id": 0})
    if not existing:
        await asyncio.to_thread(
            _telegram_api,
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": "Record not found.", "show_alert": True},
        )
        return

    if existing.get("handled"):
        await asyncio.to_thread(
            _telegram_api,
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": "Already marked as handled ✓",
                "show_alert": False,
            },
        )
        return

    await db.contacts.update_one(
        {"id": contact_id},
        {"$set": {
            "handled": True,
            "handled_at": handled_at.isoformat(),
            "handled_by": actor,
        }},
    )

    # Acknowledge the callback with a toast
    try:
        await asyncio.to_thread(
            _telegram_api,
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": "Marked as handled ✓",
                "show_alert": False,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"answerCallbackQuery failed: {e}")

    # Edit the message: append "Handled" footer, keep only Reply button
    gmail_compose_url = (
        "https://mail.google.com/mail/?view=cm&fs=1"
        f"&to={existing.get('email','')}"
        "&su=Re:%20Your%20enquiry%20to%20House%20of%20IHM"
    )
    ts = handled_at.strftime("%d %b %Y · %H:%M UTC")

    # Rebuild the text with handled footer
    try:
        # Use editMessageReplyMarkup to swap button set
        await asyncio.to_thread(
            _telegram_api,
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": f"✅ Handled · {ts}", "callback_data": "noop"}],
                        [{"text": "✉️ Reply by email", "url": gmail_compose_url}],
                    ]
                },
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"editMessageReplyMarkup failed: {e}")

    logger.info(f"Contact {contact_id} marked as handled via {actor}")


@api_router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates — only callback_query 'handle:<id>' is actioned."""
    if TELEGRAM_WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != TELEGRAM_WEBHOOK_SECRET:
            logger.warning("Rejected Telegram webhook with bad secret token")
            raise HTTPException(status_code=401, detail="unauthorized")

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json")

    cq = update.get("callback_query")
    if not cq:
        return {"ok": True}

    data = (cq.get("data") or "").strip()
    callback_id = cq.get("id")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    actor = "telegram:" + str((cq.get("from") or {}).get("username") or (cq.get("from") or {}).get("id") or "unknown")

    if data.startswith("handle:"):
        contact_id = data.split(":", 1)[1]
        await _handle_mark_handled(contact_id, callback_id, chat_id, message_id, actor)
    elif data == "noop":
        try:
            await asyncio.to_thread(
                _telegram_api,
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "Already handled ✓"},
            )
        except Exception:  # noqa: BLE001
            pass

    return {"ok": True}


@api_router.post("/telegram/setup-webhook")
async def telegram_setup_webhook():
    """One-off registration of the Telegram webhook. Call this after deploying."""
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN missing")
    if not BACKEND_PUBLIC_URL:
        raise HTTPException(status_code=400, detail="BACKEND_PUBLIC_URL missing")
    webhook_url = f"{BACKEND_PUBLIC_URL}/api/telegram/webhook"
    try:
        result = await asyncio.to_thread(
            _telegram_api,
            "setWebhook",
            {
                "url": webhook_url,
                "secret_token": TELEGRAM_WEBHOOK_SECRET,
                "allowed_updates": ["callback_query"],
            },
        )
        logger.info(f"Telegram webhook set to {webhook_url}: {result}")
        return {"ok": True, "webhook_url": webhook_url, "telegram_response": result}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to register Telegram webhook: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"message": "House of IHM EdTech API"}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(**input.model_dump())
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.status_checks.insert_one(doc)
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check.get('timestamp'), str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


@api_router.post("/contact", response_model=Contact)
async def create_contact(payload: ContactCreate):
    # Duplicate-protection: block same email submissions within DUPLICATE_WINDOW_SECONDS
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    ).isoformat()
    recent = await db.contacts.find_one(
        {"email": payload.email, "created_at": {"$gte": cutoff}},
        {"_id": 0, "id": 1, "created_at": 1},
    )
    if recent:
        raise HTTPException(
            status_code=429,
            detail="You've just sent a message — please wait a moment before sending another.",
        )

    contact_obj = Contact(**payload.model_dump())
    doc = contact_obj.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    try:
        await db.contacts.insert_one(doc)
    except Exception as e:
        logger.error(f"Failed to insert contact: {e}")
        raise HTTPException(status_code=500, detail="Failed to save your message. Please try again.")
    # Fire-and-forget notifications — never block the response on failure
    asyncio.create_task(_send_contact_notification(contact_obj))
    asyncio.create_task(_send_auto_reply(contact_obj))
    asyncio.create_task(_send_telegram_alert(contact_obj))
    return contact_obj


@api_router.get("/contact", response_model=List[Contact])
async def list_contacts():
    contacts = await db.contacts.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for c in contacts:
        if isinstance(c.get('created_at'), str):
            c['created_at'] = datetime.fromisoformat(c['created_at'])
    return contacts


@api_router.post("/admin/login")
async def admin_login(creds: HTTPBasicCredentials = Depends(_http_basic)):
    """Validate admin credentials. Returns 200 on success, 401 otherwise.
    Frontend caches the basic-auth header locally and uses it for protected endpoints."""
    username = require_admin(creds)
    return {"ok": True, "username": username}


@api_router.get("/contact/log")
async def contact_log(limit: int = 20, _admin: str = Depends(require_admin)):
    """Lightweight delivery log of the last N contact submissions with email status."""
    limit = max(1, min(limit, 100))
    rows = (
        await db.contacts.find({}, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
        .to_list(limit)
    )
    log = []
    for r in rows:
        log.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "email": r.get("email"),
                "message": (r.get("message") or "")[:280],
                "timestamp": r.get("created_at"),
                "notification_status": r.get("notification_status", "unknown"),
                "notification_email_id": r.get("notification_email_id"),
                "autoreply_status": r.get("autoreply_status", "unknown"),
                "autoreply_email_id": r.get("autoreply_email_id"),
                "telegram_status": r.get("telegram_status", "unknown"),
                "telegram_message_id": r.get("telegram_message_id"),
                "priority_tag": r.get("priority_tag"),
                "handled": bool(r.get("handled")),
                "handled_at": r.get("handled_at"),
                "handled_by": r.get("handled_by"),
            }
        )
    return {"count": len(log), "submissions": log}


# Include router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
