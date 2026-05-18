-- handler/snapshot의 application_sid VARCHAR(36) → VARCHAR(64).
-- 2026-05-18 신진섭. POSTGRES_15.
-- 사유: client는 'SID-{UUID 36자}' = 40자 형태로 전송. 기존 36자 컬럼에서 truncation 503 발생.
-- handler/claim 실패 케이스 확인됨 (matching-ops-api-00018-r5s, KST 10:55).
-- memo·llm_insight_cache는 이미 64라 일관성 맞춤. row 0건이라 즉시 완료.

ALTER TABLE matching_ops_handler
    ALTER COLUMN application_sid TYPE VARCHAR(64);

ALTER TABLE matching_ops_application_snapshot
    ALTER COLUMN application_sid TYPE VARCHAR(64);
