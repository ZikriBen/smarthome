from h11 import SERVER
from pydantic_settings import BaseSettings


class Settings(BaseSettings):

    IR_CODES_FILE: str = "data/ir_codes.json"
    LOG_LEVEL : str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"  # This will ignore extra fields instead of throwing errors


settings = Settings()
