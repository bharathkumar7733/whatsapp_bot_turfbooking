from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import List


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_whatsapp_number: str  # e.g. whatsapp:+14155238886

    # Groq
    groq_api_key: str = ""

    # Turf
    turf_name: str = "My Turf"
    turf_location: str = "Chennai"
    turf_open_hour: int = 6
    turf_close_hour: int = 23
    turf_price_per_slot: int = 600
    advance_amount: int = 300
    upi_id: str = "yourname@upi"

    # Owners — raw comma-separated string
    owner_numbers: str = ""

    # App
    app_env: str = "development"

    @property
    def owner_list(self) -> List[str]:
        """Returns list of owner WhatsApp numbers in whatsapp:+91XXXXXXXXXX format."""
        return [n.strip() for n in self.owner_numbers.split(",") if n.strip()]

    def is_owner(self, phone: str) -> bool:
        return phone in self.owner_list

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
