-- matching-ops snapshot — subjects + wage_ranges JSONB 컬럼 추가
-- 2026-05-18 신진섭. teacher_specialties (pipe-delimited subject_wage id) → 한글 과목명 + DesiredCost 라벨
-- 비정규화 보존. 관리 신청서 카드 첫 칩 + 시급 범위 표시.

ALTER TABLE matching_ops_application_snapshot
    ADD COLUMN IF NOT EXISTS subjects    JSONB,
    ADD COLUMN IF NOT EXISTS wage_ranges JSONB;
