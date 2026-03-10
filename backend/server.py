#!/usr/bin/env python3
"""
MeshCore Email Gateway - Web Management Backend v1.0
"""

import asyncio
import json
import logging
import re
import smtplib
import email as email_lib
from email.message import EmailMessage
from email.utils import parseaddr
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    import aioimaplib
    HAS_IMAP = True
except ImportError:
    HAS_IMAP = False
    print("Warning: aioimaplib not installed, IMAP IDLE disabled")

try:
    from meshcore import MeshCore, EventType
    HAS_MESHCORE = True
except ImportError:
    HAS_MESHCORE = False
    print("Warning: meshcore not installed, running in SIMULATION mode")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    gstate._loop = asyncio.get_running_loop()
    yield

app = FastAPI(title="MeshCore Email Gateway", version="1.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_here = Path(__file__).parent
frontend_path = None
for candidate in [_here.parent / "frontend", _here / "frontend", _here]:
    if (candidate / "index.html").exists():
        frontend_path = candidate
        break

# ═══════════════════════════════════════════════════════════
# 配置模型
# ═══════════════════════════════════════════════════════════

CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_ERROR_FORMATS = {
    "invalid-subject": {
        "subject": "Message delivery failed - Invalid subject format",
        "body": (
            'Subject must be a 6-char hex node prefix (e.g. "a1b2c3").\n'
            'Your subject: "{{user_subject}}"\n\n'
            "Known nodes:\n{{known_nodes}}\n\n"
            "Please retry with the correct subject format."
        ),
        "enabled": True,
    },
    "node-not-found": {
        "subject": "Message delivery failed - Node not found",
        "body": (
            'No RF node found with prefix "{{node_prefix}}".\n\n'
            "Known nodes:\n{{known_nodes}}\n\n"
            "Check the node prefix and try again."
        ),
        "enabled": True,
    },
    "msg-too-long": {
        "subject": "Message delivery failed - Message too long",
        "body": (
            "Your message is too long for RF transmission.\n\n"
            "Message size: {{msg_bytes}} bytes\n"
            "RF limit: {{max_bytes}} bytes\n\n"
            "Please shorten your message and try again."
        ),
        "enabled": True,
    },
    "delivery-fail": {
        "subject": "Message delivery failed - RF transmission error",
        "body": (
            "Failed to deliver your message to {{node_name}} ({{node_prefix}}) over RF.\n\n"
            "This may be because the node is out of range or offline.\n"
            "Please try again later."
        ),
        "enabled": True,
    },
    # ── NEW: RF → Email reply format ──────────────────────────────────────
    "rf-reply": {
        "subject": "RF message from {{node_name}}",
        "body": (
            "{{node_name}} says:\n{{rf_message}}\n\n"
            "────────────────────────────────────────\n"
            "To reply: email {{gateway_email}} with Subject: {{node_prefix}}"
        ),
        "enabled": True,
    },
}

DEFAULT_CONFIG = {
    "connection_type": "ble",
    "ble_address": "",
    "ble_pin": "",
    "serial_port": "/dev/ttyUSB0",
    "serial_baudrate": 115200,
    "tcp_host": "192.168.1.100",
    "tcp_port": 4000,
    "tcp_auto_reconnect": True,
    "agent_email": "",
    "smtp_server": "smtp.qq.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": True,
    "imap_server": "imap.qq.com",
    "imap_port": 993,
    "imap_user": "",
    "imap_password": "",
    "rf_max_message_bytes": 200,
    "idle_timeout": 1740,
    "debug": True,
    "error_formats": DEFAULT_ERROR_FORMATS,
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            cfg = {**DEFAULT_CONFIG, **saved}
            # Merge error_formats sub-keys rather than replacing wholesale
            ef = {**DEFAULT_ERROR_FORMATS, **(saved.get("error_formats") or {})}
            cfg["error_formats"] = ef
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


class ConfigModel(BaseModel):
    connection_type: str = "ble"
    ble_address: str = ""
    ble_pin: str = ""
    serial_port: str = "/dev/ttyUSB0"
    serial_baudrate: int = 115200
    tcp_host: str = "192.168.1.100"
    tcp_port: int = 4000
    tcp_auto_reconnect: bool = True
    agent_email: str = ""
    smtp_server: str = "smtp.qq.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    imap_server: str = "imap.qq.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    rf_max_message_bytes: int = 200
    idle_timeout: int = 1740
    debug: bool = True
    error_formats: Optional[dict] = None  # if None, keep existing


class SendEmailRequest(BaseModel):
    to_prefix: str         # 6-char hex prefix of target RF node
    from_email: str        # sender email address shown in RF packet
    body: str              # message body


# ═══════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════

class GatewayState:
    def __init__(self):
        self.running = False
        self.rf_connected = False
        self.imap_connected = False
        self.meshcore = None
        self.agent = None
        self.stats = {"email_to_rf": 0, "rf_to_email": 0, "errors": 0, "contacts": 0}
        self.contacts: list[dict] = []
        self.log_buffer: list[dict] = []
        self.ws_clients: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.ws_clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.remove(ws)

    def add_log(self, level: str, message: str):
        entry = {
            "type": "log",
            "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "level": level,
            "message": message,
        }
        self.log_buffer.append(entry)
        if len(self.log_buffer) > 1000:
            self.log_buffer = self.log_buffer[-1000:]
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.broadcast(entry), loop)
            except RuntimeError:
                pass


gstate = GatewayState()


class WSLogHandler(logging.Handler):
    def emit(self, record):
        gstate.add_log(record.levelname, self.format(record))


def setup_logger(debug: bool):
    logger = logging.getLogger("mesh_email_gateway")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()
    h = WSLogHandler()
    h.setFormatter(logging.Formatter("%(name)s - %(message)s"))
    logger.addHandler(h)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(sh)
    return logger


# ═══════════════════════════════════════════════════════════
# Email Agent
# ═══════════════════════════════════════════════════════════

class EmailAgent:
    AUTO_REPLY_PREFIXES = (
        "message delivery failed",
        "rf message from",
        "auto:",
        "automatic reply",
        "out of office",
        "undelivered mail",
        "delivery status notification",
    )

    def __init__(self, meshcore, cfg: dict):
        self.meshcore = meshcore
        self.cfg = cfg
        self.running = True
        self.logger = logging.getLogger("mesh_email_gateway")

    def _err_fmt(self, key: str) -> dict:
        """Return the error format config for a given key, falling back to defaults."""
        ef = self.cfg.get("error_formats") or {}
        return {**DEFAULT_ERROR_FORMATS.get(key, {}), **(ef.get(key) or {})}

    def _render_err_body(self, key: str, **kwargs) -> tuple[str, str]:
        """Return (subject, body) for an error key with variables substituted."""
        fmt = self._err_fmt(key)
        subj = fmt.get("subject", "Message delivery failed")
        body = fmt.get("body", "")
        for var, val in kwargs.items():
            body = body.replace("{{" + var + "}}", str(val))
            subj = subj.replace("{{" + var + "}}", str(val))
        return subj, body

    async def start(self):
        self._fetch_lock = asyncio.Lock()
        if HAS_MESHCORE:
            from meshcore import EventType
            self.meshcore.subscribe(EventType.CONTACT_MSG_RECV, self.handle_incoming_mesh_message)
            # get_contacts() can return None on timeout — guard against it
            try:
                result = await self.meshcore.commands.get_contacts()
                if result is not None and result.type != EventType.ERROR:
                    payload = result.payload
                    if isinstance(payload, dict):
                        # CONTACTS event payload is {pubkey: contact_dict, ...}
                        # Filter out metadata keys (non-dict values like 'lastmod')
                        gstate.contacts = [v for v in payload.values() if isinstance(v, dict)]
                    elif isinstance(payload, list):
                        gstate.contacts = payload
                    else:
                        gstate.contacts = []
                    gstate.stats["contacts"] = len(gstate.contacts)
                    self.logger.info(f"Loaded {len(gstate.contacts)} contacts")
                elif result is None:
                    self.logger.warning("get_contacts() timed out — continuing without contact list")
                else:
                    self.logger.warning(f"get_contacts() returned error: {result.payload}")
            except Exception as e:
                self.logger.warning(f"get_contacts() exception: {e} — continuing without contact list")
            await self.meshcore.start_auto_message_fetching()
        self.logger.info("Auto message fetching started")
        asyncio.create_task(self.imap_idle_loop())
        self.logger.info("IMAP IDLE listener started")

    async def stop(self):
        self.running = False
        if HAS_MESHCORE and self.meshcore:
            await self.meshcore.stop_auto_message_fetching()
            await self.meshcore.disconnect()

    async def imap_idle_loop(self):
        if not HAS_IMAP:
            self.logger.warning("aioimaplib not installed, IMAP IDLE disabled")
            return

        import inspect
        cfg = self.cfg
        while self.running:
            client = None
            try:
                self.logger.info("Connecting to IMAP server (IDLE mode)...")
                client = aioimaplib.IMAP4_SSL(host=cfg["imap_server"], port=cfg["imap_port"])
                await client.wait_hello_from_server()

                login_res = await client.login(cfg["imap_user"], cfg["imap_password"])
                login_ok = (
                    str(login_res[0]).upper() == "OK"
                    if isinstance(login_res, (list, tuple))
                    else str(getattr(login_res, "result", "")).upper() == "OK"
                )
                if not login_ok:
                    self.logger.error(
                        f"IMAP login FAILED (result={login_res}). "
                        "Check imap_user / imap_password. For QQ Mail use the Auth-Code."
                    )
                    gstate.imap_connected = False
                    await gstate.broadcast({"type": "status", "imap": False})
                    await asyncio.sleep(30)
                    continue

                select_res = await client.select("INBOX")
                select_ok = (
                    str(select_res[0]).upper() == "OK"
                    if isinstance(select_res, (list, tuple))
                    else str(getattr(select_res, "result", "")).upper() == "OK"
                )
                if not select_ok:
                    self.logger.error(f"IMAP SELECT INBOX failed: {select_res}")
                    await asyncio.sleep(10)
                    continue

                gstate.imap_connected = True
                await gstate.broadcast({"type": "status", "imap": True})
                self.logger.info("IMAP login OK, entering IDLE loop")
                await self._fetch_unseen(client)

                while self.running:
                    self.logger.debug("Sending IDLE...")
                    await client.idle_start()
                    try:
                        responses = await asyncio.wait_for(
                            client.wait_server_push(), timeout=cfg["idle_timeout"]
                        )
                        self.logger.debug(f"IDLE push: {responses}")
                    except asyncio.TimeoutError:
                        self.logger.debug("IDLE timeout, renewing...")
                        responses = []

                    done_result = client.idle_done()
                    if done_result is not None and inspect.isawaitable(done_result):
                        await done_result

                    try:
                        reselect = await asyncio.wait_for(client.select("INBOX"), timeout=10)
                        reselect_ok = (
                            str(reselect[0]).upper() == "OK"
                            if isinstance(reselect, (list, tuple))
                            else str(getattr(reselect, "result", "")).upper() == "OK"
                        )
                        if not reselect_ok:
                            self.logger.warning("IMAP re-SELECT failed after IDLE, reconnecting...")
                            break
                    except Exception as e:
                        self.logger.warning(f"IMAP re-SELECT error after IDLE ({e}), reconnecting...")
                        break

                    has_new = any(
                        b"EXISTS" in r if isinstance(r, bytes) else "EXISTS" in str(r)
                        for r in responses
                    )
                    if has_new:
                        if self._fetch_lock.locked():
                            self.logger.debug("Fetch already in progress, skipping duplicate trigger")
                        else:
                            self.logger.info("New mail detected, fetching...")
                            async with self._fetch_lock:
                                await self._fetch_unseen(client)

                try:
                    await client.logout()
                except Exception:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                gstate.imap_connected = False
                await gstate.broadcast({"type": "status", "imap": False})
                self.logger.error(f"IMAP IDLE error: {e}", exc_info=True)
                if client is not None:
                    try:
                        await client.logout()
                    except Exception:
                        pass
                await asyncio.sleep(10)

        gstate.imap_connected = False
        await gstate.broadcast({"type": "status", "imap": False})

    async def _fetch_unseen(self, client):
        try:
            typ, data = await asyncio.wait_for(
                client.protocol.search("UNSEEN", charset=None), timeout=15
            )
        except (asyncio.TimeoutError, TimeoutError) as e:
            self.logger.warning(f"IMAP SEARCH timed out — connection likely stale, will reconnect: {e}")
            raise
        except Exception as e:
            self.logger.error(f"_fetch_unseen error: {e}", exc_info=True)
            raise

        if typ != "OK":
            return
        raw = data[0]
        id_list = raw.split() if isinstance(raw, bytes) else (raw.split() if raw else [])
        if not id_list:
            return
        self.logger.info(f"Processing {len(id_list)} unseen email(s)")
        for num in id_list:
            num_str = num.decode() if isinstance(num, bytes) else str(num)
            try:
                typ, msg_data = await asyncio.wait_for(client.fetch(num_str, "(RFC822)"), timeout=30)
            except (asyncio.TimeoutError, TimeoutError) as e:
                self.logger.warning(f"IMAP FETCH timed out for msg {num_str}, will reconnect: {e}")
                raise
            except Exception as e:
                self.logger.error(f"IMAP FETCH error for msg {num_str}: {e}", exc_info=True)
                raise

            if typ != "OK":
                continue
            raw_email = next(
                (bytes(item) for item in msg_data if isinstance(item, (bytes, bytearray)) and len(item) > 200),
                None,
            )
            if not raw_email:
                continue
            email_message = email_lib.message_from_bytes(raw_email)
            await self._process_single_email(client, num_str, email_message)

    async def _process_single_email(self, client, num_str, email_message):
        from_header = email_message.get("From", "")
        from_addr = parseaddr(from_header)[1]
        if not from_addr:
            await client.store(num_str, "+FLAGS", "\\Seen")
            return

        subject = email_message.get("Subject", "").strip()
        await client.store(num_str, "+FLAGS", "\\Seen")

        agent_email = self.cfg.get("agent_email", "").lower()
        if from_addr.lower() == agent_email:
            self.logger.debug(f"Skipping self-sent email: subject={subject!r}")
            return

        subject_lower = subject.lower()
        if any(subject_lower.startswith(p) for p in self.AUTO_REPLY_PREFIXES):
            self.logger.debug(f"Skipping auto-reply email: subject={subject!r}")
            return

        body = self._get_email_body(email_message)
        self.logger.info(f"Email from={from_addr}, subject={subject!r}")

        subject_match = re.match(r"^([0-9a-fA-F]{6})$", subject)
        if not subject_match:
            ef = self._err_fmt("invalid-subject")
            if ef.get("enabled", True):
                subj, err_body = self._render_err_body(
                    "invalid-subject",
                    user_subject=subject,
                    known_nodes=self._known_nodes_str(),
                )
                await asyncio.to_thread(self._send_email_sync, from_addr, subj, err_body)
            gstate.stats["errors"] += 1
            await gstate.broadcast({"type": "stats", "stats": gstate.stats})
            return

        prefix6 = subject_match.group(1).lower()
        message_text = body.strip()
        contact = self._find_contact_by_prefix(prefix6)

        if not contact:
            self.logger.warning(f"No contact with prefix6='{prefix6}'")
            ef = self._err_fmt("node-not-found")
            if ef.get("enabled", True):
                subj, err_body = self._render_err_body(
                    "node-not-found",
                    node_prefix=prefix6,
                    known_nodes=self._known_nodes_str(),
                )
                await asyncio.to_thread(self._send_email_sync, from_addr, subj, err_body)
            gstate.stats["errors"] += 1
            await gstate.broadcast({"type": "stats", "stats": gstate.stats})
            return

        mesh_msg = f"{from_addr} {message_text}"
        max_bytes = self.cfg["rf_max_message_bytes"]
        if len(mesh_msg.encode("utf-8")) > max_bytes:
            self.logger.warning("Message too long")
            ef = self._err_fmt("msg-too-long")
            if ef.get("enabled", True):
                subj, err_body = self._render_err_body(
                    "msg-too-long",
                    msg_bytes=len(mesh_msg.encode("utf-8")),
                    max_bytes=max_bytes,
                )
                await asyncio.to_thread(self._send_email_sync, from_addr, subj, err_body)
            gstate.stats["errors"] += 1
            await gstate.broadcast({"type": "stats", "stats": gstate.stats})
            return

        success = await self._send_mesh_message(contact, mesh_msg)
        if success:
            gstate.stats["email_to_rf"] += 1
            self.logger.info(f"✅ Email from {from_addr} → mesh node {contact.get('adv_name')}")
        else:
            ef = self._err_fmt("delivery-fail")
            node_name = contact.get("adv_name", prefix6)
            if ef.get("enabled", True):
                subj, err_body = self._render_err_body(
                    "delivery-fail",
                    node_name=node_name,
                    node_prefix=prefix6,
                )
                await asyncio.to_thread(self._send_email_sync, from_addr, subj, err_body)
            gstate.stats["errors"] += 1
            self.logger.error(f"❌ Failed to deliver to {node_name}")
        await gstate.broadcast({"type": "stats", "stats": gstate.stats})

    async def handle_incoming_mesh_message(self, event):
        """Handle RF → Email: use the configurable 'rf-reply' format."""
        payload = event.payload
        text = payload.get("text", "")
        sender_key_prefix = payload.get("pubkey_prefix", "unknown")
        self.logger.info(f"📡 RF message from {sender_key_prefix}: {text!r}")
        if not text.startswith("#"):
            return
        match = re.match(r"^#\s*([^\s]+@[^\s]+\.[^\s]+)\s+(.*)$", text, re.DOTALL)
        if not match:
            return
        recipient_email, message_body = match.groups()
        contact = self.meshcore.get_contact_by_key_prefix(sender_key_prefix) if HAS_MESHCORE else None
        node_name = contact.get("adv_name", sender_key_prefix) if contact else sender_key_prefix
        prefix6 = (contact.get("public_key", sender_key_prefix) if contact else sender_key_prefix)[:6].lower()

        ef = self._err_fmt("rf-reply")
        if not ef.get("enabled", True):
            self.logger.debug(f"RF reply suppressed by config for {sender_key_prefix}")
            return

        # Use the configurable subject/body template
        subject, body = self._render_err_body(
            "rf-reply",
            node_name=node_name,
            node_prefix=prefix6,
            rf_message=message_body,
            gateway_email=self.cfg.get("agent_email", ""),
        )

        ok = await asyncio.to_thread(self._send_email_sync, recipient_email, subject, body)
        if ok:
            gstate.stats["rf_to_email"] += 1
            self.logger.info(f"✅ Forwarded RF message to {recipient_email}")
        else:
            gstate.stats["errors"] += 1
            self.logger.error(f"❌ Failed to forward RF message to {recipient_email}")
        await gstate.broadcast({"type": "stats", "stats": gstate.stats})

    def _find_contact_by_prefix(self, prefix6):
        for c in gstate.contacts:
            if c.get("public_key", "").lower().startswith(prefix6):
                return c
        return None

    def _known_nodes_str(self):
        lines = [f"  {c.get('adv_name','?')} → {c.get('public_key','')[:6]}" for c in gstate.contacts]
        return "\n".join(lines) if lines else "  (no contacts)"

    def _get_email_body(self, email_message):
        def decode_payload(payload, charset_hint):
            for enc in ([charset_hint] if charset_hint else []) + ["gbk", "gb2312", "utf-8"]:
                try:
                    return payload.decode(enc)
                except (LookupError, UnicodeDecodeError):
                    continue
            return payload.decode("utf-8", errors="ignore")

        if email_message.is_multipart():
            for part in email_message.walk():
                if part.get_content_type() == "text/plain":
                    p = part.get_payload(decode=True)
                    if p:
                        return decode_payload(p, part.get_content_charset()).strip()
        else:
            p = email_message.get_payload(decode=True)
            if p:
                return decode_payload(p, email_message.get_content_charset()).strip()
        return ""

    def _send_email_sync(self, to_addr, subject, body) -> bool:
        import ssl as _ssl
        cfg = self.cfg
        host     = cfg["smtp_server"]
        port     = int(cfg["smtp_port"])
        user     = cfg["smtp_user"]
        password = cfg["smtp_password"]
        use_tls  = cfg["smtp_use_tls"]

        msg = EmailMessage()
        msg.set_content(body, charset="utf-8")
        msg["Subject"] = subject
        msg["From"]    = cfg["agent_email"]
        msg["To"]      = to_addr

        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE

        def _try_ssl():
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
                s.ehlo(); s.login(user, password); s.send_message(msg)

        def _try_starttls():
            with smtplib.SMTP(host, 587, timeout=15) as s:
                s.ehlo(); s.starttls(context=ctx); s.ehlo(); s.login(user, password); s.send_message(msg)

        def _try_plain():
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo(); s.login(user, password); s.send_message(msg)

        if port == 465:
            attempts = [("SSL/465", _try_ssl), ("STARTTLS/587", _try_starttls)]
        elif use_tls:
            attempts = [("STARTTLS/587", _try_starttls), ("SSL/465", _try_ssl)]
        else:
            attempts = [("plain", _try_plain), ("STARTTLS/587", _try_starttls), ("SSL/465", _try_ssl)]

        last_err = None
        for label, fn in attempts:
            try:
                fn()
                self.logger.info(f"✅ Email sent ({label}) → {to_addr}")
                return True
            except smtplib.SMTPAuthenticationError as e:
                self.logger.error(f"❌ SMTP auth error ({label}): {e}")
                return False
            except Exception as e:
                self.logger.debug(f"SMTP {label} failed: {e}")
                last_err = e

        self.logger.error(f"❌ All SMTP attempts failed. Last error: {last_err}")
        return False

    async def _send_mesh_message(self, contact, message) -> bool:
        if not HAS_MESHCORE:
            self.logger.info(f"[SIM] Would send to {contact.get('adv_name','?')}: {message}")
            return True
        try:
            from meshcore import EventType
            result = await self.meshcore.commands.send_msg(contact, message)
            if result.type == EventType.ERROR:
                self.logger.error(f"send_msg returned ERROR: {result.payload}")
                return False
            ack = result.payload.get("expected_ack")
            if ack:
                ack_str = ack.hex() if isinstance(ack, (bytes, bytearray)) else str(ack)
                self.logger.debug(f"MSG_SENT ack={ack_str}")
                return True
            return result.type != EventType.ERROR
        except Exception as e:
            self.logger.error(f"Exception sending mesh message: {e}")
            return False

    async def send_manual_email(self, to_prefix: str, from_email: str, body: str) -> tuple[bool, str]:
        """Called by /api/send_email — compose and send via gateway SMTP."""
        contact = self._find_contact_by_prefix(to_prefix)
        if not contact:
            return False, f"No RF node found with prefix '{to_prefix}'"

        mesh_msg = f"{from_email} {body}"
        max_bytes = self.cfg["rf_max_message_bytes"]
        if len(mesh_msg.encode("utf-8")) > max_bytes:
            return False, f"Message too long ({len(mesh_msg.encode())} bytes, limit {max_bytes})"

        node_name = contact.get("adv_name", to_prefix)
        success = await self._send_mesh_message(contact, mesh_msg)
        if success:
            gstate.stats["email_to_rf"] += 1
            await gstate.broadcast({"type": "stats", "stats": gstate.stats})
            self.logger.info(f"✅ Manual send from {from_email} → mesh node {node_name} ({to_prefix})")
            return True, f"Message delivered to {node_name} ({to_prefix})"
        else:
            gstate.stats["errors"] += 1
            await gstate.broadcast({"type": "stats", "stats": gstate.stats})
            self.logger.error(f"❌ Manual send failed → {node_name}")
            return False, f"RF delivery to {node_name} failed"


# ═══════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════

@app.get("/")
async def root():
    if frontend_path and (frontend_path / "index.html").exists():
        return FileResponse(str(frontend_path / "index.html"))
    return {"message": "MeshCore Gateway API — frontend not found", "docs": "/docs"}


@app.get("/api/config")
async def get_config():
    return load_config()


@app.post("/api/config")
async def set_config(cfg: ConfigModel):
    existing = load_config()
    data = {**existing, **{k: v for k, v in cfg.dict().items() if v is not None}}
    if cfg.error_formats is not None:
        data["error_formats"] = {**DEFAULT_ERROR_FORMATS, **cfg.error_formats}
    save_config(data)
    # Hot-reload error_formats in running agent
    if gstate.agent:
        gstate.agent.cfg = data
    return {"ok": True}


@app.post("/api/connect")
async def connect():
    if gstate.running:
        raise HTTPException(400, "Already running")

    cfg = load_config()
    setup_logger(cfg["debug"])
    logger = logging.getLogger("mesh_email_gateway")

    gstate.running = True
    gstate.rf_connected = False
    gstate.imap_connected = False
    await gstate.broadcast({"type": "status", "running": True, "rf": False, "imap": False})

    async def _connect():
        try:
            if HAS_MESHCORE:
                ct = cfg["connection_type"]
                logger.info(f"Connecting via {ct}...")
                if ct == "serial":
                    mc = await MeshCore.create_serial(cfg["serial_port"], cfg["serial_baudrate"], debug=cfg["debug"])
                elif ct == "tcp":
                    mc = await MeshCore.create_tcp(cfg["tcp_host"], cfg["tcp_port"], debug=cfg["debug"], auto_reconnect=cfg["tcp_auto_reconnect"])
                else:
                    mc = await MeshCore.create_ble(cfg["ble_address"] or None, pin=cfg["ble_pin"], debug=cfg["debug"])
                gstate.meshcore = mc
            else:
                logger.info("[SIM] MeshCore not installed — simulation mode")
                gstate.meshcore = None

            gstate.rf_connected = True
            logger.info("✅ Connected to MeshCore")
            await gstate.broadcast({"type": "status", "running": True, "rf": True, "imap": False})

            agent = EmailAgent(gstate.meshcore, cfg)
            gstate.agent = agent
            await agent.start()
            await gstate.broadcast({"type": "status", "running": True, "rf": True, "imap": True, "simulation": not HAS_MESHCORE})

        except Exception as e:
            gstate.running = False
            gstate.rf_connected = False
            gstate.imap_connected = False
            gstate.meshcore = None
            gstate.agent = None
            logger.error(f"Connection failed: {e}")
            await gstate.broadcast({"type": "status", "running": False, "rf": False, "imap": False, "error": str(e)})

    asyncio.create_task(_connect())
    return {"ok": True, "message": "Connecting..."}


@app.post("/api/disconnect")
async def disconnect():
    if not gstate.running:
        raise HTTPException(400, "Not running")
    gstate.running = False
    if gstate.agent:
        await gstate.agent.stop()
        gstate.agent = None
    gstate.rf_connected = False
    gstate.imap_connected = False
    await gstate.broadcast({"type": "status", "running": False, "rf": False, "imap": False})
    logging.getLogger("mesh_email_gateway").info("Email gateway stopped.")
    return {"ok": True}


@app.get("/api/status")
async def get_status():
    return {
        "running": gstate.running,
        "rf": gstate.rf_connected,
        "imap": gstate.imap_connected,
        "stats": gstate.stats,
        "contacts": gstate.contacts,
        "simulation": not HAS_MESHCORE,
    }


@app.get("/api/contacts")
async def get_contacts():
    return {"contacts": gstate.contacts, "count": len(gstate.contacts)}


@app.get("/api/logs")
async def get_logs(limit: int = 200):
    return {"logs": gstate.log_buffer[-limit:]}


@app.post("/api/test/smtp")
async def test_smtp():
    cfg = load_config()
    try:
        def _test():
            msg = EmailMessage()
            msg.set_content("MeshCore Gateway SMTP test — connection OK.")
            msg["Subject"] = "MeshCore Gateway Test"
            msg["From"] = cfg["agent_email"]
            msg["To"] = cfg["agent_email"]
            with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
                if cfg["smtp_use_tls"]:
                    server.starttls()
                server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.send_message(msg)
        await asyncio.to_thread(_test)
        return {"ok": True, "message": f"Test email sent to {cfg['agent_email']}"}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/send_email")
async def send_email(req: SendEmailRequest):
    """
    Manually compose and send a message to an RF node via the gateway.
    The gateway must be running (connected) to use this endpoint.
    """
    if not gstate.running or not gstate.agent:
        raise HTTPException(400, "Gateway not connected. Click Connect first.")

    # Validate prefix
    if not re.match(r"^[0-9a-fA-F]{6}$", req.to_prefix):
        raise HTTPException(400, "to_prefix must be a 6-character hex string")

    ok, message = await gstate.agent.send_manual_email(
        req.to_prefix.lower(), req.from_email, req.body
    )
    if ok:
        return {"ok": True, "message": message}
    else:
        raise HTTPException(500, message)


# ═══════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    gstate.ws_clients.append(ws)
    await ws.send_json({
        "type": "init",
        "status": {
            "running": gstate.running,
            "rf": gstate.rf_connected,
            "imap": gstate.imap_connected,
            "simulation": not HAS_MESHCORE,
        },
        "stats": gstate.stats,
        "logs": gstate.log_buffer[-100:],
        "contacts": gstate.contacts,
    })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in gstate.ws_clients:
            gstate.ws_clients.remove(ws)


# ═══════════════════════════════════════════════════════════
# Static Files (mount last so it doesn't shadow /api/*)
# ═══════════════════════════════════════════════════════════

if frontend_path:
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
    print(f"Serving frontend from: {frontend_path}")
else:
    print("WARNING: frontend/index.html not found.")

# ═══════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run("server:app", port=8765, reload=True)
