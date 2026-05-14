# matching-ops-api

자란다 매칭 운영 대시보드(`matching-ops` Cloud Run)의 백엔드 API.
자란다 read replica MySQL을 읽기 전용으로 조회.

## 패턴

- vibe-cs / auto-call과 동일하게 Cloud Run + VPC connector로 read replica 직접 접근
- 쓰기 작업 없음 (모든 endpoint는 GET)
- 인증: Google OAuth (@jaranda.kr만 허용)
- CORS는 페이지 도메인만 허용

## 엔드포인트

| Method | Path | 인증 | 설명 |
|--------|------|------|------|
| GET | `/` | — | 서비스 정보 |
| GET | `/health` | — | 헬스 체크 |
| GET | `/api/auth/config` | — | Google client_id 공개값 |
| POST | `/api/auth/google` | — | Google credential → 세션 토큰 |
| GET | `/api/auth/me` | Bearer | 현재 세션 정보 |
| GET | `/api/applications?limit=30` | Bearer | 최근 신청서 목록 |
| GET | `/api/applications/{sid}` | Bearer | 단건 상세 |
| GET | `/api/applications/{sid}/teachers` | Bearer | 지원/요청 선생님 목록 |

페이지에서 `Authorization: Bearer <token>` 헤더로 호출.

## 로컬 실행

```bash
cp .env.example .env
# .env에 JARANDA_REPLICA_URL 비밀번호 + GOOGLE_CLIENT_ID 채우기

# Cloud SQL Proxy로 로컬에서 replica 접근 가능
cloud-sql-proxy platform-jaranda-kr-standby:asia-northeast1:platform-jaranda-kr-standby-replica --port=3308 &

# .env의 호스트를 127.0.0.1:3308로 변경 후 실행
pip install -e .
uvicorn src.main:app --reload --port 8080
```

## Cloud Run 배포

별도 Cloud Run 서비스(`matching-ops-api`)로 띄우고, 페이지(`matching-ops`)에서 CORS로 호출.

```bash
# 1) 컨테이너 빌드 (backend 디렉토리에서)
gcloud builds submit \
  --tag asia-northeast3-docker.pkg.dev/platform-jaranda-kr-standby/cloud-run-source-deploy/matching-ops-api:latest \
  --project platform-jaranda-kr-standby

# 2) 배포 (vibe-cs-connector 재사용)
gcloud run deploy matching-ops-api \
  --image asia-northeast3-docker.pkg.dev/platform-jaranda-kr-standby/cloud-run-source-deploy/matching-ops-api:latest \
  --region asia-northeast3 \
  --project platform-jaranda-kr-standby \
  --vpc-connector vibe-cs-connector \
  --vpc-egress all-traffic \
  --update-env-vars JARANDA_REPLICA_URL='mysql+asyncmy://siahn:비번@10.6.16.6:3306/jaranda',ALLOWED_ORIGINS='https://matching-ops-266295307740.asia-northeast3.run.app',GOOGLE_CLIENT_ID='...apps.googleusercontent.com',OTP_SECRET_KEY='랜덤32byteshex',AUTH_REQUIRED='true' \
  --allow-unauthenticated \
  --max-instances 3

# 3) URL 확인
gcloud run services describe matching-ops-api --region asia-northeast3 --format='value(status.url)'
```

**주의 (메모리 참조):**
- `--update-env-vars` 사용 (`--set-env-vars` 금지 — 2026-04-10 사고)
- 비밀번호는 vibe-cs와 동일값 (2026-04-20 fynn 변경, Secret Manager 이관 미진행)
- 배포자 자체 수정 범위: 빌드 에러/오타는 OK, 로직·토큰 변경은 개발자 승인 필요 (2026-04-13 절차)
- 배포 전·후 `git log origin/main..HEAD` 0건 + working dir clean 검증 (2026-04-30 drift 사고)

## Google OAuth 설정

1. GCP 콘솔 → APIs & Services → Credentials → "Create Credentials" → OAuth 2.0 Client ID
2. Application type: Web application
3. Authorized JavaScript origins:
   - `https://matching-ops-266295307740.asia-northeast3.run.app`
   - `http://localhost:8000`
4. 생성된 Client ID를 `GOOGLE_CLIENT_ID` 환경변수에 등록

## 페이지 연결

페이지(`../index.html`)는 다음 URL로 API 호출 (별도 PR로 추가 예정):
```
https://matching-ops-api-266295307740.asia-northeast3.run.app
```

## 향후 작업 (현재 backend가 채우지 못해 mock 또는 null 처리하는 필드)

| 필드 | 데이터 소스 |
|------|------|
| `prob` (매칭확률) | LLM 예측 — 별도 파이프라인 |
| `appCount`/`confirmedCount`/`lessonCount` (누적 이력) | parent_account_sid 기준 집계 쿼리 |
| `visitsAfter` (작성 후 앱 진입 횟수) | 이벤트 분석 (BigQuery / GA) |
| `viewed` (선생님 프로필 열람 여부) | 이벤트 시스템 |
| `totalHours`/`subjectHours`/`rating`/`reviewCount` (선생님 활동정보) | teacher_stat / review 테이블 |
| `timeline` (액션 타임라인) | recommendation_log + alimtalk_send_history + 이벤트 |
