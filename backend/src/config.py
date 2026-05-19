from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    jaranda_replica_url: str

    # matching-ops 전용 PostgreSQL (메모/인사이트 영속화). 미설정 시 메모 API 비활성.
    # 형식: postgresql+asyncpg://user:pw@/db?host=/cloudsql/PROJECT:REGION:INSTANCE
    matching_ops_db_url: str = ""

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

    # 마감 임박 임계값(분). DeadlineTag urgent/soon 분기.
    urgent_threshold_min: int = 240  # 4시간
    soon_threshold_min: int = 1440  # 24시간

    # 신청서 조회 윈도우 (시간). 기본 7일.
    recent_window_hours: int = 168

    # LLM 인사이트 — Anthropic Claude Sonnet 4.6. 키 미설정 시 인사이트 API 비활성.
    anthropic_api_key: str = ""
    llm_model_id: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 512
    llm_daily_limit: int = 200

    # Firestore (자란다 채팅방 직접 조회). 미설정 시 채팅방 신호 비활성 (graceful, 응답 필드 null).
    # 자란다 prod Firestore 프로젝트 = "platform-firebase-chat" (별도 GCP project).
    # matching-ops-api는 platform-jaranda-kr-standby에 떠있으므로 cross-project IAM 필요:
    #   gcloud projects add-iam-policy-binding platform-firebase-chat \
    #     --member="serviceAccount:<matching-ops-api SA>" --role="roles/datastore.viewer"
    firestore_project: str = ""
    firestore_enabled: bool = False

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
