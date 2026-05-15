-- matching-ops 처리 담당(handler) 영속화 — Phase 1
-- 2026-05-15 신진섭. 신청서당 1명 자연 강제(PK), 본인만 잡고/해제 가능.

CREATE TABLE IF NOT EXISTS matching_ops_handler (
    application_sid VARCHAR(36)  PRIMARY KEY,
    handler_email   VARCHAR(255) NOT NULL,
    handler_name    VARCHAR(70),
    claimed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

GRANT ALL ON matching_ops_handler TO matching_ops_app;
