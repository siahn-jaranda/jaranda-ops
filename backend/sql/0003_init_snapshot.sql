-- matching-ops 신청서 스냅샷 — Phase 1
-- 2026-05-15 신진섭. 메모가 있는 신청서의 영속 이력 보존.
-- 자란다 prod에서 신청서가 사라져도 운영팀이 이력을 유지할 수 있도록 함.

CREATE TABLE IF NOT EXISTS matching_ops_application_snapshot (
    application_sid     VARCHAR(36)  PRIMARY KEY,
    child_name          VARCHAR(70),
    region              VARCHAR(100),
    status_key          VARCHAR(20),
    status_label        VARCHAR(20),
    request_chips       JSONB,
    parent_request      TEXT,
    matched_teacher     VARCHAR(70),
    cancelled_reason    TEXT,
    is_urgent           BOOLEAN,
    auto_confirm        BOOLEAN,
    re_recommend        BOOLEAN,
    app_created_at      TIMESTAMPTZ,
    app_deadline_at     TIMESTAMPTZ,
    app_confirmed_at    TIMESTAMPTZ,
    app_cancelled_at    TIMESTAMPTZ,
    snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- list_managed가 메모 카운트 + 최근 메모 작성순으로 정렬하므로 별도 인덱스 불필요
-- (matching_ops_memo 쪽 idx_memo_app가 application_sid + created_at 커버)

GRANT ALL ON matching_ops_application_snapshot TO matching_ops_app;
