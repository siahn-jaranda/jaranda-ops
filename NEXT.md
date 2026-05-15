# matching-ops 다음 작업

작성: 2026-05-14 (배포 직후)
배포 리비전: backend `matching-ops-api-00010-q88`, frontend `matching-ops-00008-4wz`

## 0. 배포 직후 모니터링 (2026-05-15 오전 가장 먼저)

- [ ] 24h 5xx 발생률 — `gcloud run services logs read matching-ops-api --region asia-northeast3 --project platform-jaranda-kr-standby --limit 100 | grep -iE "error|exception|503"`
- [ ] LEFT JOIN 변경으로 인한 정책 미연결 row 노출량 점검 — 화면에 "정책 미연결" sub가 비정상적으로 많이 보이는지
- [ ] parent 누적 이력 쿼리 응답시간 — list 30건 호출 시 IN 절 expand로 인한 지연 여부 (Cloud Run logs latency 확인)
- [ ] 운영팀 피드백 — "추정" 배지 / disabled 버튼 / 빈 상태 화면 UX 받아들여졌는지

## 1. 메모 / 태그 영속화 (P2 → P0 승격 필요)

현재 메모 작성하면 페이지 새로고침 시 소실. 즉시 후속.

**결정 필요**:
- write DB 위치 — 옵션 A) 자란다 prod write DB 신규 테이블 (DBA 협의), B) Cloud SQL 별도 인스턴스, C) Firestore
- 권장: **B 옵션 (Cloud SQL 별도 인스턴스, vibe-cs DB와 분리)** — 자란다 prod 영향 0, 비용 ~$10/월

**구현 범위**:
- 신규 테이블 `matching_ops_memo (id, application_sid, author_email, content, tags JSON, created_at, updated_at)`
- 백엔드 라우트:
  - `POST /api/applications/{sid}/memos` — 작성
  - `GET /api/applications/{sid}/memos` — 조회
  - `DELETE /api/applications/{sid}/memos/{id}` — 본인 글만
- 프론트:
  - `saveMemo()` → 백엔드 호출 + 작성 즉시 리스트 갱신
  - 인사이트 탭에 application별 메모 카운트 + 최근 글 표시
- env: `MATCHING_OPS_WRITE_DB_URL` 추가

## 2. LLM 인사이트 실호출 (메모 영속화 후)

- 메모가 N건 이상 쌓인 application에서만 활성화
- Anthropic API (Claude Haiku 4.5 또는 Sonnet 4.6)
- 프롬프트: 메모 + 신청서 컨텍스트 → 핵심 패턴/제안 액션 추출
- 캐싱: 동일 application의 메모 변동 없으면 5분 캐시
- 비용 가드: 일일 호출 한도 + 응답 길이 제한
- env: `ANTHROPIC_API_KEY` 추가

## 3. 선생님 활동정보 조인

현재 응답에서 제거된 6필드(`totalHours/subject/subjectHours/rating/reviewCount/viewed`) 복원.

**테이블/컬럼 확인 필요**:
- `teacher_stat` 또는 유사 — 누적 강의시간 집계
- `review` 또는 유사 — 평점/리뷰수
- `viewed` — 프로필 열람 이벤트 (BigQuery? Cloud Logging?)

`db.list_recommendation_teachers()` 쿼리에 LEFT JOIN 추가 또는 별도 batch fetch.

## 4. Timeline / viewedSummary

- `recommendation_log` — 추천 액션 히스토리
- `alimtalk_send_history` — 알림톡 전송 이력
- 이벤트 시스템 — 부모님 앱 진입/열람 (BigQuery `events_*` 추정)

응답에 `timeline: [{at, actor, action, detail}]` 추가.

## 5. 휴리스틱 prob 정확도 측정 (선택)

- 신규 신청서가 충분히 쌓이면 (~500건) `prob` 추정값 vs 실제 confirmed 매칭률 비교
- 정확도 낮으면 LLM 예측 모듈 우선순위 ↑
- 측정 스크립트만 미리 짜두면 좋음 (BigQuery 또는 ad-hoc SQL)

## 6. 잡일 / 정리

- [ ] CSS의 `.data-source-pill` 룰 (line 441-446) 제거 — 더 이상 사용 안 함
- [ ] `setDataSource` no-op 함수 (line 647) 제거 — 호출처 없음
- [ ] `nginx.conf`에 `/api/` → backend proxy 규칙 검토 (현재는 프론트가 `API_BASE` 절대 URL로 호출 → CORS 의존)
- [ ] backend `Dockerfile`에 healthcheck 추가
- [ ] Secret Manager 이관 — JARANDA_REPLICA_URL 비밀번호 평문이 env에 노출 (vibe-cs와 동일 사안)

## 7. 미해결/모호한 항목

- **Status 코드 50, 60 등 미정의 코드** 실제 데이터에서 발견되는지 — 현재는 "etc" + "상태50" 같은 라벨로 노출. recommendation_status 메모리(10/20/40/90/99/100/101)가 전부인지 확인 필요
- **칸반 → 카드뷰 자동 전환** 운영팀이 어색해하면 칸반 그대로 두고 사이드시트/모달 방식으로 변경 검토
- **mock APPS 제거 후 첫 진입 시 빈 화면** — Google 로그인 안 되어 있으면 게이트가 뜨므로 정상이지만, 로그인 후 0건이면 어색. 운영팀 피드백 받아 안내문 다듬기
