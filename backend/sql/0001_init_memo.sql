-- matching-ops 메모 영속화 Phase 1 초기 스키마
-- 2026-05-15 신진섭. POSTGRES_15. DB=matching_ops, app user=matching_ops_app.

CREATE TABLE IF NOT EXISTS matching_ops_memo (
    id              BIGSERIAL PRIMARY KEY,
    application_sid VARCHAR(64)  NOT NULL,
    author_email    VARCHAR(255) NOT NULL,
    author_name     VARCHAR(64),
    content         TEXT         NOT NULL,
    tags            JSONB        NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memo_app
    ON matching_ops_memo (application_sid, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memo_author
    ON matching_ops_memo (author_email, created_at DESC)
    WHERE deleted_at IS NULL;

GRANT ALL ON matching_ops_memo TO matching_ops_app;
GRANT USAGE, SELECT ON SEQUENCE matching_ops_memo_id_seq TO matching_ops_app;
