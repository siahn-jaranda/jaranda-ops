-- A/B 3-arm variant 의미 v2 갱신 (2026-06-15)
-- 1차 결과: 기존 V1(R1+R2+A1+A6+A2)이 매칭률 15%로 베이스(3.6%) 대비 4.2배
--           → 기존 V1을 새 베이스로 승격, A2(요일)+A6(성별)을 공통 베이스로 끌어올림
--
-- 새 정의 (이 마이그레이션 이후 기록되는 variant):
--   0 = 신규 V0 = R1+R2 + A1(시급) + A2(요일) + A6(성별) ← 이전 V1과 동치 (챔피언)
--   1 = 신규 V1 = R1+R2 + A2(요일) + A6(성별)            ← 시급 marginal 측정용
--   2 = 신규 V2 = R1+R2 + A4(같은 구) + A2(요일) + A6(성별) ← 시급↔거리 trade-off
--
-- 분석 시 주의: 배포 시점 이전 row 와 이후 row 의 variant 의미가 다름.
-- run_at < 배포 timestamp 인 row 는 구 정의(0=베이스/1=+요일/2=+같은구) 로 해석.

COMMENT ON COLUMN matching_ops_auto_run.variant IS
  'A/B 3-arm v2 (2026-06-15~): 0=base+wage(이전V1), 1=base only (시급 빼고 요일만), 2=base+same_gu(시급 대신 거리). base=R1+R2+A2(요일)+A6(성별)';
