from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    jaranda_replica_url: str

    # CORS — 페이지 도메인. 운영은 Cloud Run, 로컬은 file://·localhost
    allowed_origins: str = (
        "https://matching-ops-266295307740.asia-northeast3.run.app,"
        "http://localhost:8000,http://localhost:3000,http://127.0.0.1:8000"
    )

    # 인증: Google OAuth (@jaranda.kr 만 허용). auto-call 패턴 동일.
    google_client_id: str = ""
    auth_required: bool = True
    otp_secret_key: str = ""
    otp_session_hours: int = 8

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
