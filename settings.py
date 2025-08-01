from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables (.env file)."""

    FIRECRAWL_API_KEY: str
    GROQ_API_KEY: str

    # Configure how settings are loaded
    model_config = SettingsConfigDict(
        env_file=".env",  
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


# Singleton instance to be imported across the project
settings = Settings() 