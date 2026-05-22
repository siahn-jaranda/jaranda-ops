# 지원 0개 신청서 선생님 추천 LLM (WELL2-100 PoC)

[WELL2-100](https://jaranda.atlassian.net/browse/WELL2-100) 기획안의 Phase 1 구현 PoC.
지원(applied) 선생님이 0명인 신청서에 "지원할 만한" 선생님 후보를 찾아 운영팀에 제시한다.

## 설계: Retrieval → LLM 2단계

LLM에게 선생님을 "찾으라"고 시키지 않는다. 후보는 **SQL/룰로 먼저 좁히고**, LLM은
주어진 후보 안에서만 순위·사유를 만든다 → 없는 선생님을 추천하는 할루시네이션 차단.

```
신청서(sid) → ① Retrieval(SQL) → 후보 K명 → ② LLM 랭킹/사유 → 운영팀 대시보드
```

### ① Retrieval (검증된 실데이터 기반)
1. **부모 좌표 → 인접 시군구**: `recommendation.lat/lng`를 `service_area_geometry`의
   시군구 중심좌표와 `ST_Distance_Sphere`로 비교해 가까운 N개 구. (부모의 행정구역
   코드 컬럼 `parent_service_area_code`는 NULL이라 좌표만 신뢰)
2. **그 지역 선호 선생님**: `teacher_preference_service_area`(선생님이 등록한 활동
   희망 지역, **매일 갱신되는 살아있는 데이터**)에서 후보 선생님.
   *주의: `udong_teacher_list`는 2024-08 이후 갱신 중단 → 사용 안 함.*
3. **필터**: `activity_status` 활동중(2)→부족 시 활동대기(10) / 지원불가(3)·탈퇴(4)
   제외 / 이미 이 신청서에 추천·거절된 선생님 제외(`recommendation_teachers`).
4. **신호 결합**: 가용요일(`schedule`), 평가·추천율(`parent_feedback`), 현재 담당
   아이 수(`visit_instance` status=1, 여력), 과목 시급, 과목별 자기소개 텍스트.
5. 선호지역 priority·추천수·경력순 상위 K명.

### ② LLM
- 모델/패턴은 기존 인사이트(`llm_client.py`)와 동일: system prompt `cache_control`,
  JSON 강제, 일일 한도·캐시 재사용.
- "완벽 fit"이 아니라 **기본 조건(동네·요일) 충족 + 지원 의향**이 생길 만한지로 랭킹.
- 출력: `summary` / `ranked[{teacher_sid, name, rank, reason, caution}]` / `note`.

## 파일
- `zero_applicant_recommender.py` — Retrieval + LLM 한 모듈. backend 통합 시
  `Recommender.retrieve_candidates`는 `db.py`로, `SYSTEM_PROMPT`는 `llm_client.py`로,
  엔드포인트는 `routes/candidates.py`로 옮긴다.
- `SAMPLE_OUTPUT.md` — 실제 신청서 1건(종로·영어) end-to-end 결과.

## 실행 (backend 환경 = Cloud Run, 키·replica 보유)
```bash
export JARANDA_REPLICA_URL=...   # backend/.env.cloud
export ANTHROPIC_API_KEY=...     # Cloud Run 환경변수에만 존재 (로컬엔 없음)
python poc/zero_applicant_recommender.py <recommendation_sid>
```

## 한계 / 다음 단계
- 시군구 중심좌표 기준이라 구 경계 근처는 약간 부정확 → 동(법정동) 단위 정밀화 여지.
- 요일까지만 매칭, 시간대(비트마스크 30bit) 정밀 매칭은 v2.
- 과목 적합성은 LLM이 자기소개 텍스트로 정성 판단(시급 등록은 모든 과목에 있어 하드
  필터 불가). 필요 시 `experience_hour_for_study/play`로 보강.
- Phase 2: 생성 후 n시간 지원 0건 자동 트리거 + 선생님 타겟 메시지 초안.
- Phase 3: 콘솔 쓰기 API로 PUSH 자동 발송(별도 승인) + 발송→지원 전환 측정.
