-- matching-ops snapshot — subjects 컬럼 추가
-- 2026-05-18 신진섭. recommendation.teacher_specialties (pipe-delimited category id)
-- 를 한글 이름으로 해석한 결과를 비정규화 저장. 카드 칩 첫 자리에 노출.

ALTER TABLE matching_ops_application_snapshot
    ADD COLUMN IF NOT EXISTS subjects VARCHAR(200);
