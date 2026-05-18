-- matching-ops snapshot — subjects + wage_ranges JSONB 컬럼 추가
-- 2026-05-18 신진섭. teacher_specialties (pipe-delimited subject_wage id) → 한글 과목명 + DesiredCost 라벨
-- 비정규화 보존. 관리 신청서 카드 첫 칩 + 시급 범위 표시.
--
-- 같은 날 오전에 잘못된 매핑(request_form_category 기준)으로 추가됐던
-- subjects VARCHAR(200) 컬럼을 먼저 DROP. revert 391b8f4와 함께 정리 ([[reference_matching_ops_db]]).

ALTER TABLE matching_ops_application_snapshot
    DROP COLUMN IF EXISTS subjects;

ALTER TABLE matching_ops_application_snapshot
    ADD COLUMN IF NOT EXISTS subjects    JSONB,
    ADD COLUMN IF NOT EXISTS wage_ranges JSONB;
