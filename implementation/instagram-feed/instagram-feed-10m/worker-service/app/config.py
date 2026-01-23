from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/instagram_db"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"
    celebrity_threshold: int = 10000
    fan_out_batch_size: int = 1000

    class Config:
        env_file = ".env"


settings = Settings()
