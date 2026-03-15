from __future__ import annotations

try:
    import boto3
except Exception:  # pragma: no cover - optional import guard
    boto3 = None

from app.config import get_settings


class SesEmailService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.enabled = all(
            [
                self.settings.ses_region,
                self.settings.ses_from_email,
                self.settings.amazon_key_id,
                self.settings.amazon_key_secret,
            ]
        )
        self.client = None
        if self.enabled and boto3 is not None:
            self.client = boto3.client(
                "ses",
                region_name=self.settings.ses_region,
                aws_access_key_id=self.settings.amazon_key_id,
                aws_secret_access_key=self.settings.amazon_key_secret,
            )

    def send_email(
        self,
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> dict:
        if not self.enabled:
            raise ValueError(
                "Amazon SES is not configured. Set SES_REGION, SES_FROM_EMAIL, AMAZON_KEY_ID, and AMAZON_KEY_SECRET."
            )
        if self.client is None:
            raise ValueError("boto3 is unavailable. Install dependencies before sending email.")

        destination = to_email.strip()
        if not destination:
            raise ValueError("Destination email is required.")

        message = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": text_body, "Charset": "UTF-8"}},
        }
        if html_body:
            message["Body"]["Html"] = {"Data": html_body, "Charset": "UTF-8"}

        response = self.client.send_email(
            Source=self.settings.ses_from_email,
            Destination={"ToAddresses": [destination]},
            Message=message,
        )
        return {"message_id": response.get("MessageId")}
