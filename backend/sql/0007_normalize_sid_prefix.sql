-- application_sid 'SID-{uuid}' → '{uuid}' 정규화.
-- 2026-05-19 신진섭. POSTGRES_15.
-- 사유: handlers.py(71d1a4d) / memos.py·insights.py(이번) 라우트가 모두
--      raw uuid 키로 통일됨. 기존 'SID-' 접두사 row가 있으면 mismatch로
--      화면에 안 보임. 마이그레이션으로 prefix 제거.
-- 적용 순서: 라우트 정규화 배포 직후. 미배포 상태에서 적용 시 신규 row가
--          'SID-' 형태로 다시 들어올 수 있음.

BEGIN;

UPDATE matching_ops_handler
   SET application_sid = SUBSTRING(application_sid FROM 5)
 WHERE application_sid LIKE 'SID-%';

UPDATE matching_ops_memo
   SET application_sid = SUBSTRING(application_sid FROM 5)
 WHERE application_sid LIKE 'SID-%';

UPDATE matching_ops_application_snapshot
   SET application_sid = SUBSTRING(application_sid FROM 5)
 WHERE application_sid LIKE 'SID-%';

UPDATE matching_ops_llm_insight_cache
   SET application_sid = SUBSTRING(application_sid FROM 5)
 WHERE application_sid LIKE 'SID-%';

COMMIT;
