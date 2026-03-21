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
    <body style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 30px; background-color: #f9f9f9;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 20px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.05); border: 1px solid #f0f0f0;">
            <div style="background: linear-gradient(135deg, #a855f7 0%, #6366f1 100%); padding: 40px 20px; text-align: center;">
                <h1 style="color: #ffffff; margin: 0; font-size: 28px; font-weight: 900; letter-spacing: -1px;">Welcome to TelegramCrmAi!</h1>
                <p style="color: rgba(255,255,255,0.8); margin: 10px 0 0; font-weight: 500;">Let's get your secure environment ready.</p>
            </div>
            <div style="padding: 40px; text-align: center;">
                <p style="color: #444; font-size: 16px; line-height: 1.6; margin-bottom: 30px;">
                    Thank you for choosing the world's most powerful Telegram CRM. To complete your activation and protect your account, please enter the following verification code:
                </p>
                
                <div style="background: #f5f3ff; padding: 25px; border-radius: 16px; display: inline-block; font-size: 36px; font-weight: 900; letter-spacing: 12px; color: #a855f7; border: 1px dashed rgba(168, 85, 247, 0.3); margin-bottom: 30px;">
                    {otp}
                </div>
                
                <p style="color: #888; font-size: 13px; line-height: 1.5;">
                    This security code is active for <b>10 minutes</b>.<br/>
                    If you did not initiate this registration, please ignore this email.
                </p>
            </div>
            <div style="background: #fafafa; padding: 20px; text-align: center; border-top: 1px solid #f0f0f0;">
                <p style="color: #aaa; font-size: 12px; margin: 0;">
                    &copy; 2026 TelegramCrmAi. Professional Telegram Automation & CRM.
                </p>
            </div>
        </div>
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
