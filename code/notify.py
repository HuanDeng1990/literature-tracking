#!/usr/bin/env python3
"""
Literature Tracker — Notification Module
Sends a macOS banner notification and optionally an email digest.
"""

import logging
import os
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def send_macos_notification(title: str, message: str, subtitle: str = ""):
    """Post a native macOS notification banner."""
    script_parts = [f'display notification "{message}"']
    script_parts.append(f'with title "{title}"')
    if subtitle:
        script_parts.append(f'subtitle "{subtitle}"')
    # Play the default notification sound
    script_parts.append('sound name "Glass"')
    script = " ".join(script_parts)
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
        log.info("macOS notification sent.")
    except subprocess.CalledProcessError as e:
        log.warning("macOS notification failed: %s", e.stderr.decode().strip())


def open_file_in_default_app(filepath: str):
    """Open a file with the default macOS application."""
    try:
        subprocess.run(["open", filepath], check=True, capture_output=True)
        log.info("Opened %s", filepath)
    except subprocess.CalledProcessError as e:
        log.warning("Failed to open file: %s", e)


def send_email(subject: str, body_md: str):
    """Send the weekly reading list as an email (plain text + markdown body)."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    email_cfg = cfg.get("notification", {}).get("email", {})
    if not email_cfg.get("enabled"):
        return

    smtp_server = email_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = email_cfg.get("smtp_port", 587)
    sender = email_cfg.get("sender", "")
    recipient = email_cfg.get("recipient", "")
    password_env = email_cfg.get("password_env", "LIT_TRACKER_EMAIL_PWD")
    password = os.environ.get(password_env, "")

    if not all([sender, recipient, password]):
        log.warning(
            "Email not configured. Set sender, recipient in config.yaml "
            "and %s environment variable.",
            password_env,
        )
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body_md, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        log.info("Email sent to %s", recipient)
    except Exception as e:
        log.error("Email failed: %s", e)


def notify(reading_list_path: str, summary: str, body_md: str = ""):
    """
    Send all configured notifications.
    - macOS banner with summary
    - Optionally open the reading list file
    - Optionally send email
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    notif_cfg = cfg.get("notification", {})

    if notif_cfg.get("macos_banner", True):
        send_macos_notification(
            title="Literature Tracker",
            message=summary,
            subtitle="Weekly Reading List Ready",
        )

    if notif_cfg.get("open_file_on_complete", True) and reading_list_path:
        open_file_in_default_app(reading_list_path)

    if notif_cfg.get("email", {}).get("enabled"):
        from datetime import datetime
        week_str = datetime.now().strftime("%b %d, %Y")
        send_email(
            subject=f"Weekly Reading List — {week_str}",
            body_md=body_md,
        )
