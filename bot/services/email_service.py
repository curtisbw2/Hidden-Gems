"""Email service using SendGrid."""
import logging
from typing import Optional
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content

logger = logging.getLogger(__name__)


class EmailService:
    """Email service wrapper for SendGrid."""
    
    def __init__(self, api_key: Optional[str], from_email: Optional[str]):
        self.api_key = api_key
        self.from_email = from_email
        self.enabled = bool(api_key and from_email)
        
        if self.enabled:
            try:
                self.sg = sendgrid.SendGridAPIClient(api_key=api_key)
            except Exception as e:
                logger.error(f"Failed to initialize SendGrid: {e}")
                self.enabled = False
        else:
            self.sg = None
    
    async def send_otp(self, to_email: str, otp_code: str) -> bool:
        """
        Send OTP code to email.
        Returns True if sent successfully, False otherwise.
        """
        if not self.enabled:
            logger.warning("Email service not configured")
            return False
        
        try:
            message = Mail(
                from_email=Email(self.from_email),
                to_emails=To(to_email),
                subject="Hidden Gems Research - Verification Code",
                plain_text_content=f"Your verification code is: {otp_code}\n\nThis code expires in 10 minutes."
            )
            
            response = self.sg.send(message)
            
            if response.status_code in (200, 201, 202):
                logger.info(f"OTP sent to {to_email}")
                return True
            else:
                logger.error(f"SendGrid returned status {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to send OTP email: {e}")
            return False
