-- A/B 4-arm variant 컬럼 추가
-- 2026-06-02 신진섭. POSTGRES_15.
-- V0=베이스(R1+R2+A1), V1=+A2(요일), V2=+A4(거리5km), V3=+A2+A4+A5(full)
-- sid hash % 4 로 자동 할당. matching_ops_auto_run 분석 쿼리는 variant 그룹별 통계.

ALTER TABLE matching_ops_auto_run
  ADD COLUMN IF NOT EXISTS variant SMALLINT;

CREATE INDEX IF NOT EXISTS idx_auto_run_variant_run_at
  ON matching_ops_auto_run (variant, run_at DESC);

COMMENT ON COLUMN matching_ops_auto_run.variant IS
  'A/B variant: 0=base(R1+R2+A1), 1=+A2 day, 2=+A4 dist5km, 3=+A2+A4+A5 full';
