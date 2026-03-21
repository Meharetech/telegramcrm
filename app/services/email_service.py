import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.config import settings
import logging

def send_otp_email(to_email: str, otp: str):
    """
    Sends a 6-digit OTP email to the user for password reset.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logging.warning("SMTP credentials not set. OTP send skipped.")
        return False

    msg = MIMEMultipart()
    msg['From'] = settings.SMTP_FROM or settings.SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = "Your Password Reset OTP - TelegramCrmAi"

    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; padding: 20px;">
        <h2 style="color: #a855f7;">Password Reset Request</h2>
        <p>You requested a password reset for your TelegramCrmAi account.</p>
        <p>Your 6-digit verification code is:</p>
        <div style="background: #f5f3ff; padding: 15px; border-radius: 10px; display: inline-block; font-size: 24px; font-weight: bold; letter-spacing: 5px; color: #a855f7; border: 1px solid #e2e8f0;">
            {otp}
        </div>
        <p style="margin-top: 20px; color: #666; font-size: 14px;">This code will expire in 10 minutes.</p>
        <p style="color: #666; font-size: 14px;">If you didn't request this, you can safely ignore this email.</p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

def send_registration_otp_email(to_email: str, otp: str):
    """
    Sends a 6-digit OTP email to the user for account registration.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logging.warning("SMTP credentials not set. Registration OTP skip.")
        return False

    msg = MIMEMultipart()
    msg['From'] = settings.SMTP_FROM or settings.SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = "Welcome to TelegramCrmAi! Verify Your Email"

    body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; padding: 20px;">
        <h2 style="color: #a855f7;">Welcome to TelegramCrmAi!</h2>
        <p>Thank you for initiating your onboarding. To finalize your account creation and activate your access, please verify your email address.</p>
        <p>Your 6-digit registration code is:</p>
        <div style="background: #f5f3ff; padding: 15px; border-radius: 10px; display: inline-block; font-size: 24px; font-weight: bold; letter-spacing: 5px; color: #a855f7; border: 1px solid #e2e8f0;">
            {otp}
        </div>
        <p style="margin-top: 20px; color: #666; font-size: 14px;">This code will expire in 10 minutes. If you encounter any issues, please contact our support team.</p>
        <p style="color: #666; font-size: 14px;">Best regards,<br>The TelegramCrmAi Team</p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))

    try:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        logging.error(f"Failed to send registration email: {e}")
        return False
