from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # IMAP
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_USER: str = ""
    IMAP_PASS: str = ""
    IMAP_MAILBOX: str = "INBOX"

    # Filtering
    EMAIL_SENDER: str = ""
    EXACT_MATCH: bool = True

    # OpenAI
    OPENAI_API_KEY: str = ""

    # RSS settings
    RSS_TITLE: str = "Email RSS Feed"
    RSS_LINK: str = "http://localhost/rss"
    MAX_ITEMS: int = 50

    # Polling interval in seconds (e.g., 3600 = 1 hour)
    POLL_INTERVAL: int = 3600

    # State file
    STATE_FILE: str = "data/state.json"

    class Config:
        env_file = ".env"
        extra = "ignore"  # This will ignore extra fields instead of throwing errors


settings = Settings()
