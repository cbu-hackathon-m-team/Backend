# 구현 확인 기록

2026-10-31, Windows / Python 3.12.14 / uv 0.9.26 / Docker Desktop Linux containers.

| 확인 | 결과 |
| --- | --- |
| Ruff 검사와 포맷 | 통과 |
| 단위 테스트 | 26개 통과 |
| 실제 Docker/PostgreSQL 통합 테스트 | 9개 통과 |
| Alembic 초기 migration 및 스키마 차이 검사 | 통과 |
| API Docker 이미지 빌드, Compose 기동 | 통과 |
| 실행 중 API `/health`, `/docs`, 인증된 프로젝트 조회 | 각각 HTTP 200 |
| 인증 없는 프로젝트 조회 | HTTP 401 |
| 실제 PyPI Pydantic 버전 목록 조회 | 208개 배포 버전 반환; 조회 시점 값 |
| 실행별 테스트 컨테이너·네트워크·이미지 정리 | 잔여 실행 라벨 자원 없음 |

실제 통합 경로:

- 같은 앱의 Pydantic 1.10.24 → 2.12.5를 uv로 변경하고 A/B 이미지에 실제 설치.
- 업무 소스 변경 없이 HTTP 200 + 배송 호출 1회 → 0회 차이를 `REGRESSION_SUSPECTED`로 탐지.
- 1.10.24 → 1.10.24 대조군은 `PASS`.
- 기존 앱의 기대값을 틀리게 지정한 경우 `BASELINE_INVALID`, 후보 미실행.
- 기동 확인 실패는 `STARTUP_TIMEOUT`, `NOT_EVALUATED`, 로그 보존 후 정리.
- API 중복 요청, 입력 스냅샷 보존, 원자적 작업 획득, 동일 Runner 중단 복구와 정리 실패 복구 확인.
- 메일 응답 유실 후 같은 key/payload 재시도·제공업체 접수 상태 확인은 HTTP 테스트 대역 사용.
- 기준/후보 요청 시간 초과는 업무 회귀와 구분해 `NOT_EVALUATED`로 판정.
- PyPI 버전 확인 중 프로젝트가 바뀌면 `PROJECT_CHANGED`로 접수를 거절하고 오래된 조건으로 작업을 만들지 않음.
- 프론트엔드 브라우저에서 프로젝트 등록 → 검사 → 설정 저장 → 테스트 승인 → 실제 A/B 실행·결과 조회 확인. 동일 버전 4개 통과, 버전 변경 시 배송 호출 1 → 0 회귀 확인.

자동화된 통합 시험은 **GitHub 전송 대신 저장소에 포함된 샘플 소스**를 넣는다.
uv·Docker·HTTP·WireMock·DB·마이그레이션은 실제이고, GitHub API 형식 및 AI/메일 오류 처리는 테스트 대역이다.
Python 기준 프로그램은 재고 응답과 배송 요청만 검증하며 실제 재고 DB·배송 완료를 검증하지 않는다.

추가 수동 점검: 공개 `cbu-hackathon-m-team/OSSGuard-Purchase-Demo`의 실제 커밋 `e860635bdb292ca1c38a554b4f2c9b451ea20ac0`을 GitHub API로 확인하고 Git으로 복제해 실행했다. 로컬 대시보드에서 등록·검사·설정 저장·테스트 승인·검증 시작을 수행했으며, 두 실행 모두 `REGRESSION_SUSPECTED`와 자원 정리 `SUCCEEDED`를 확인했다. Windows의 읽기 전용 Git 객체 정리 오류를 수정한 뒤 대기 실행이 이어서 완료됐다. 작업 폴더 바깥 경로 거부는 유지한다.

미확인: GCP VM/IAM/네트워크,
Secret Manager 실제 비밀 읽기, 유료 AI 응답, Resend 실제 메일 수신, 운영 배포.
GitHub `origin`은 지정한 Backend 저장소이며 현재 작업 브랜치는 `main`이다.
