"""Email notifications through Gmail (or any SMTP/IMAP provider), Python standard library only.

* Sending: SMTP over SSL with a Gmail *app password*, retries with backoff.
* Commands: the agent reads your inbox (IMAP) for emails YOU send with a subject like
  "flights status", "flights pause", "flights resume" (Spanish works too: "vuelos pausa").
  The links under each alert open a pre-filled email; you just press Send.
"""
from __future__ import annotations

import email
import imaplib
import logging
import random
import re
import smtplib
import socket
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from html import unescape
from urllib.parse import quote

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|rv|res)\s*:\s*)*(?:flights?|vuelos?)\s+"
    r"(status|estado|pause|pausa|pausar|resume|reanudar|seguir|help|ayuda)\b", re.I)
COMMAND_MAP = {"estado": "status", "pausa": "pause", "pausar": "pause", "reanudar": "resume",
               "seguir": "resume", "ayuda": "help"}
TAG_RE = re.compile(r"<[^>]+>")


class EmailError(Exception):
    pass


def parse_command(subject: str) -> str | None:
    m = COMMAND_RE.match(subject or "")
    if not m:
        return None
    word = m.group(1).lower()
    return COMMAND_MAP.get(word, word)


def to_plain(html_text: str) -> str:
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>',
                  lambda m: m.group(2) if unescape(m.group(2)) == unescape(m.group(1)) else f"{m.group(2)} ({m.group(1)})",
                  html_text)
    return unescape(TAG_RE.sub("", text))


class EmailNotifier:
    def __init__(self, cfg: dict, address: str | None, app_password: str | None, to: str | None = None,
                 dry_run: bool = False, attempts: int = 4, base_delay: float = 3.0):
        ec = cfg.get("notifications", {}).get("email", {})
        self.smtp_host = ec.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = int(ec.get("smtp_port", 465))
        self.imap_host = ec.get("imap_host", "imap.gmail.com")
        self.commands_enabled = bool(ec.get("commands_enabled", True))
        self.prefix = ec.get("subject_prefix", "[Flights]")
        self.address = (address or "").strip() or None
        self.password = (app_password or "").replace(" ", "") or None   # Google shows it in 4 groups of 4
        # EMAIL_TO may hold one address or several separated by commas; default = the sending address
        self.to_list = [a.strip() for a in (to or "").split(",") if a.strip()] or \
            ([self.address] if self.address else [])
        self.to = ", ".join(self.to_list)
        self.dry_run = dry_run
        self.attempts = attempts
        self.base_delay = base_delay
        self.failures: list[str] = []   # main.py exits non-zero if this is not empty

    @property
    def configured(self) -> bool:
        return bool(self.address and self.password)

    # ------------------------------------------------------------------ building
    def command_link(self, cmd: str) -> str:
        addr = self.address or "you@example.com"
        return f"mailto:{addr}?subject={quote('flights ' + cmd)}&body={quote('Just press Send.')}"

    def _build(self, text: str, buttons: list[list[dict]] | None, subject: str | None) -> EmailMessage:
        first = to_plain(text.split("\n", 1)[0]).strip()
        subject = f"{self.prefix} {subject or first}".strip()
        links_html, links_plain = [], []
        for row in buttons or []:
            for b in row:
                if "url" in b:
                    href = b["url"]
                    style = ("display:inline-block;padding:10px 16px;margin:4px 6px 4px 0;background:#1a73e8;"
                             "color:#fff;text-decoration:none;border-radius:6px;font-weight:bold")
                else:
                    href = self.command_link(b["callback_data"])
                    style = ("display:inline-block;padding:8px 14px;margin:4px 6px 4px 0;border:1px solid #1a73e8;"
                             "color:#1a73e8;text-decoration:none;border-radius:6px")
                links_html.append(f'<a href="{href}" style="{style}">{b["text"]}</a>')
                links_plain.append(f'{b["text"]}: {href}')
        html_body = (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#202124">'
            + text.replace("\n", "<br>")
            + ("<br><br>" + " ".join(links_html) if links_html else "")
            + '<br><br><span style="color:#888;font-size:12px">Commands: email yourself '
              '"flights status", "flights pause" or "flights resume" (or use the buttons).</span></div>'
        )
        plain = to_plain(text) + ("\n\n" + "\n".join(links_plain) if links_plain else "")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("Flight deal agent", self.address or "agent@example.com"))
        msg["To"] = self.to or "you@example.com"
        msg["Message-ID"] = make_msgid(domain="flight-deal-agent")
        msg.set_content(plain)
        msg.add_alternative(html_body, subtype="html")
        return msg

    # ------------------------------------------------------------------ sending
    def send(self, text: str, buttons: list[list[dict]] | None = None, subject: str | None = None) -> bool:
        msg = self._build(text, buttons, subject)
        if self.dry_run or not self.configured:
            tag = "DRY-RUN" if self.dry_run else "EMAIL NOT CONFIGURED"
            print(f"\n----- [{tag}] email: {msg['Subject']} -----\n{msg.get_body(('plain',)).get_content()}")
            print("-" * 40)
            if not self.dry_run:
                self.failures.append("Email secrets missing (EMAIL_ADDRESS / EMAIL_APP_PASSWORD)")
            return self.dry_run
        last = ""
        for i in range(1, self.attempts + 1):
            try:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as s:
                    s.login(self.address, self.password)
                    s.send_message(msg)
                log.info("Email sent: %s", msg["Subject"])
                return True
            except smtplib.SMTPAuthenticationError as e:
                last = f"login rejected ({e.smtp_code}): check EMAIL_ADDRESS and the app password"
                break   # retrying won't help
            except (smtplib.SMTPException, OSError, socket.timeout) as e:
                last = f"{type(e).__name__}: {e}"
                log.warning("Email send failed (attempt %d/%d): %s", i, self.attempts, last)
                if i < self.attempts:
                    time.sleep(self.base_delay * (2 ** (i - 1)) + random.uniform(0, 1))
        err = f"Email '{msg['Subject']}' not sent: {last}"
        log.error("%s", err)
        self.failures.append(err)
        return False

    def check_login(self) -> None:
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as s:
            s.login(self.address, self.password)

    # ------------------------------------------------------------------ commands
    def read_commands(self, state: dict) -> list[str]:
        """New command emails since the last run. Never raises: a failure just means no commands."""
        if not (self.configured and self.commands_enabled) or self.dry_run:
            return []
        try:
            return self._read_commands(state)
        except (imaplib.IMAP4.error, OSError, socket.timeout) as e:
            log.warning("Could not read command emails: %s", e)
            return []

    def _read_commands(self, state: dict) -> list[str]:
        cmds: list[str] = []
        box = imaplib.IMAP4_SSL(self.imap_host, 993, timeout=30)
        try:
            box.login(self.address, self.password)
            box.select("INBOX")
            uidvalidity = (box.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
            uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)
            since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%d-%b-%Y")
            typ, data = box.uid("search", None, f'(SINCE {since} SUBJECT "flight")')
            uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
            typ2, data2 = box.uid("search", None, f'(SINCE {since} SUBJECT "vuelo")')
            if typ2 == "OK":
                uids = sorted(set(uids) | {int(x) for x in (data2[0] or b"").split()})
            last = state.get("email_last_uid")
            if last is None or state.get("email_uidvalidity") != uidvalidity:
                # first run (or mailbox reset): don't act on old emails
                state["email_last_uid"] = max(uids, default=0)
                state["email_uidvalidity"] = uidvalidity
                return []
            for uid in [u for u in uids if u > int(last)]:
                typ, msg_data = box.uid("fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                hdr = email.message_from_bytes(msg_data[0][1])
                subject = str(make_header(decode_header(hdr.get("Subject", ""))))
                sender = parseaddr(hdr.get("From", ""))[1].lower()
                cmd = parse_command(subject)
                if not cmd:
                    continue
                if sender not in {a.lower() for a in [self.address, *self.to_list] if a}:
                    log.warning("Ignoring command email from %s", sender)
                    continue
                cmds.append(cmd)
                box.uid("store", str(uid), "+FLAGS", "(\\Seen)")
            state["email_last_uid"] = max([int(last)] + uids)
        finally:
            try:
                box.logout()
            except Exception:  # noqa: BLE001
                pass
        return cmds
