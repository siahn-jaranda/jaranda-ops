# matching-ops 다음 작업

작성: 2026-05-21 (P0 검증 완료 + snapshot freeze 반영)

---

## 오늘 한 작업 (2026-05-21)

| # | 커밋/리비전 | 작업 | 상태 |
|---|------|------|------|
| 1 | `0007_normalize_sid_prefix.sql` | 운영 DB에 SID-prefix 정규화 적용 | ✅ 완료 (잔존 `SID-%` 4테이블 모두 0건 확인, UPDATE 0) |
| 2 | `603b28d` (5/19 머지분) | 매칭 카드 "💬 메모 N건" 표시 | ✅ 운영 확인 — 카드/관리탭에 정상 노출 |
| 3 | `52492b7` → main `e53b651` | snapshot을 첫 메모 시점에 freeze (덮어쓰기 방지) | ✅ 배포 완료 (matching-ops-api-00031-brw) |

---

## 3번 상세 — snapshot freeze

- **증상**: 운영팀이 관리 신청서 목록에서 본 신청서 정보가 메모 작성 당시와 달라짐.
- **원인**: `routes/memos.py _refresh_snapshot`이 매 메모 작성마다 `upsert` →
  두 번째 메모 작성 시점에 자란다 replica의 현재 상태로 `ON CONFLICT UPDATE` 덮어씀.
  첫 메모 시점 컨텍스트(시급·지역·요청사항·상태) 손실.
- **수정**:
  - `snapshot_store.insert_if_absent()` 신규 — `ON CONFLICT DO NOTHING`으로 첫 1회만 INSERT.
  - `_refresh_snapshot` → `_ensure_snapshot`으로 개명, `insert_if_absent` 호출.
- **한계**: 이미 덮어쓰여진 기존 snapshot은 회수 불가. 정책 변경 이후 첫 메모가 달리는 신청서부터 freeze 적용.
- **확인 SQL** (freeze 동작 검증 — 메모 2개 이상 단 신청서의 `snapshot_at` vs `last_memo_at` 비교):
  ```sql
  SELECT s.application_sid, s.snapshot_at, mm.first_memo_at, mm.last_memo_at, mm.cnt
  FROM matching_ops_application_snapshot s
  JOIN (SELECT application_sid, MIN(created_at) first_memo_at,
               MAX(created_at) last_memo_at, COUNT(*) cnt
        FROM matching_ops_memo WHERE deleted_at IS NULL
        GROUP BY application_sid HAVING COUNT(*) >= 2) mm
    ON mm.application_sid = s.application_sid;
  -- snapshot_at ≈ first_memo_at 이면 freeze 정상 (last_memo_at 와는 벌어짐)
  ```

---

## 현재 배포 리비전

- backend: `matching-ops-api-00031-brw` (2026-05-21)
- frontend: `matching-ops-00024-rn4` (2026-05-20, 변경 없음)

---

## 다음 작업 우선순위 후보

| 옵션 | 작업 | 호흡 | 임팩트 | 추천 시점 |
|------|------|------|--------|----------|
| **B** | Timeline / viewedSummary — 신청서 한 건의 history를 한눈에 (`recommendation_log` + `alimtalk_send_history` + 부모 앱 진입 이벤트). backend 새 엔드포인트 + frontend 사이드시트 새 섹션 | 김 | 큼 | 운영 의사결정 직결 — 메인 후속 |
| **C** | 선생님 viewed 카운트 + 평점 보강. `parent_feedback` 평점 + `teacher_profile_view.viewed_count` 활용. `_to_frontend_teacher` 필드 추가 | 짧음 | 보강 | 시간 있을 때 |
| **D** | 잡일 일괄 — `.data-source-pill` CSS 제거, `setDataSource` no-op 제거, Dockerfile healthcheck, 인사이트 페이지 mock `deriveInsights()` 정리, backend/README image-only update 절차 | 짧음 | 누적 정리 | 휴식기 |
| **E** | 휴리스틱 prob 정확도 측정 — 7일 565건 + 누적으로 추정값 vs 실 confirmed 매칭률 비교. replica/BigQuery adhoc | 중간 | 측정용 | 1~2주 누적 후 |
| **F** | 자동 새로고침 UX 후속 — 메모 모달 열렸을 때 충돌 방지, 카드 hover 깜빡임(diff render), 새로고침 도중 "갱신 중..." 인디케이터 | 중간 | UX 보강 | 1~2일 사용 피드백 후 |
| **G** | snapshot freeze 후속 — "현재 상태로 새로고침" 명시적 버튼. freeze로 진행 상태가 안 보이는 한계 보완. 운영팀이 원하면 추가 | 짧음 | 보완 | 운영 피드백 후 |

추천: **B(Timeline)** 또는 **D(잡일 일괄)** 중 선택

---

## 미해결 / 모호 항목

- status 50/60 미정의 코드 — 실데이터에서 발견되는지 모니터링
- 빈 화면 안내문 다듬기 — 현재 "최근 7일 내 매칭 신청서가 없습니다."
- Secret Manager 이관 — `MATCHING_OPS_DB_URL`, postgres 비번, `ANTHROPIC_API_KEY` 평문 노출
- snapshot freeze 한계 — 첫 메모 이후 신청서 상태 변경이 관리탭에 반영 안 됨. 진행 상태는 매칭 신청서 카드에서 확인 (옵션 G로 보완 가능)

---

## DB 스키마 버전

- 0001 init_memo
- 0002 init_handler
- 0003 init_snapshot
- 0004 alter_snapshot_subject_wage
- 0005 init_llm_insight
- 0006 alter_sid_width (VARCHAR 64자 확장)
- 0007 normalize_sid_prefix (✅ 적용 완료 2026-05-21)

---

## 핸드오프 컨텍스트

- **main HEAD**: `e53b651` 이후 (snapshot freeze + 이 NEXT.md)
- **메모리 참조**: reference_matching_ops_deploy, reference_matching_ops_db, reference_matching_ops_handler_api, feedback_frontend_syntax_check
- snapshot 정책: 첫 메모 작성 시점 freeze (`insert_if_absent`). 메모 0건 → snapshot 삭제.
