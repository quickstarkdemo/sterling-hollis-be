from __future__ import annotations

import httpx

from app.config import get_settings


class TwilioService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.enabled = all(
            [
                self.settings.twilio_account_sid,
                self.settings.twilio_api_key_sid,
                self.settings.twilio_api_key_secret,
                self.settings.twilio_sender_number,
                self.settings.twilio_test_to_number,
            ]
        )

    def send_sms(self, body: str, to_number: str | None = None) -> dict:
        if not self.enabled:
            raise ValueError(
                "Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, "
                "TWILIO_API_KEY_SECRET, TWILIO_SENDER_NUMBER, and TWILIO_TEST_TO_NUMBER."
            )

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Messages.json"
        destination = to_number or self.settings.twilio_test_to_number
        payload = {
            "To": destination,
            "From": self.settings.twilio_sender_number,
            "Body": body,
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                url,
                data=payload,
                auth=(self.settings.twilio_api_key_sid, self.settings.twilio_api_key_secret),
            )
            response.raise_for_status()
            return response.json()
