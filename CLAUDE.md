# MSP Manager — CLAUDE.md

## 프로젝트 개요
GCP(kakaoenterprise.com, ~779개 프로젝트)와 AWS(Organizations, 17개 계정)를
단일 웹 UI로 통합 관리하는 MSP 내부 운영 도구.

---

## 인프라

| 항목 | 값 |
|---|---|
| EC2 인스턴스 | `i-0a7fc7068c3acbf73` (ap-northeast-2) |
| 서버 포트 | **9070** |
| 배포 S3 버킷 | `kep-sre-config/msp-manager/` |
| EC2 접속 | `aws ssm start-session --target i-0a7fc7068c3acbf73` |
| 포트포워딩 | `aws ssm start-session --target i-0a7fc7068c3acbf73 --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["9070"],"localPortNumber":["9070"]}'` |
| 서버 로그 | `/tmp/msp-manager.log` (EC2) |

---

## 로컬 경로
```
/Users/kaiden.kim/msp-manager/
├── main.py              # FastAPI (38개 엔드포인트)
├── server.py            # uvicorn 진입점 (port 9070)
├── auth.py              # Admin/Viewer 세션 인증
├── config.py            # 환경변수
├── deploy.sh            # S3 업로드 + SSM 배포 스크립트
├── requirements.txt
├── .env                 # 실 자격증명 (git 제외)
├── gcp/
│   ├── gcp.py           # GCP API (프로젝트·빌링·IAM·리소스 스캔)
│   ├── billing.py       # GCP 빌링 비용 (BigQuery / REST)
│   ├── export.py        # Excel 내보내기
│   └── constants.py     # DELETABLE_OK / BILLING / OWNER
├── aws/services/
│   ├── aws_session.py   # boto3 Management + AssumeRole
│   ├── organizations.py # 계정 목록
│   ├── organizations_tree.py  # OU 트리 / 계정 이동
│   ├── cost_explorer.py # Cost Explorer (SP 필터 포함)
│   ├── resource_collector.py  # EC2/VPC/RDS/Lambda
│   └── cmdb.py          # CMDB CloudFront 조회 + SSM 수집 트리거
├── data/                # admin_credentials.json
└── static/index.html    # SPA (바닐라 JS, ~2100줄)
```

---

## EC2 서버 경로
```
/home/ec2-user/msp-manager/   # 로컬과 동일 구조
```

---

## 배포 방법
> **EC2에서 `aws s3 cp` 불가** (cryptography 모듈 오류) → presigned URL + curl 방식 사용

```bash
# 빠른 단일 파일 배포 (index.html 예시)
cd /Users/kaiden.kim/msp-manager
aws s3 cp static/index.html s3://kep-sre-config/msp-manager/static/index.html --region ap-northeast-2 --no-progress
INDEX_URL=$(aws s3 presign s3://kep-sre-config/msp-manager/static/index.html --expires-in 600 --region ap-northeast-2)
aws ssm send-command \
  --instance-ids "i-0a7fc7068c3acbf73" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "{\"commands\":[\"curl -s -o /home/ec2-user/msp-manager/static/index.html '${INDEX_URL}' && echo done\"]}" \
  --output text --query "Command.CommandId"

# 서버 재시작이 필요한 경우 (main.py 변경 시)
# deploy.sh 실행 또는 SSM으로 kill + nohup python3 server.py
```

### SSM 명령 완료 대기
```bash
until aws ssm get-command-invocation --command-id "$CMD_ID" \
  --instance-id "i-0a7fc7068c3acbf73" --region ap-northeast-2 \
  --query "Status" --output text 2>/dev/null | grep -qv InProgress; do sleep 3; done
```

---

## 환경변수 (.env)
```env
AWS_ACCESS_KEY_ID=<실제값은 .env 참조>
AWS_SECRET_ACCESS_KEY=<실제값은 .env 참조>
AWS_DEFAULT_REGION=ap-northeast-2
MANAGEMENT_ACCOUNT_ID=221481233822
CROSS_ACCOUNT_ROLE_NAME=AWSManagerReadOnlyRole
SECRET_KEY=msp-manager-secret-key-change-me
CMDB_URL=https://dxvqjzd9yg287.cloudfront.net/cmdb_results.json
CMDB_USER=collector
CMDB_PASS=<실제값은 .env 참조>
CMDB_COLLECTOR_INSTANCE=i-0a7fc7068c3acbf73
CMDB_COLLECTOR_PATH=/root/jobs/cmdb-collect
```

---

## 인증

### 웹 인증 (Admin/Viewer)
- **Viewer**: 로그인 없이 접속, 조회만 가능
- **Admin**: `/login` → ID: `admin` / 초기PW: `1234` → 8시간 쿠키
- Admin 전용: GCP 프로젝트 삭제, AWS 계정 이동, 설정 탭
- **중요**: Admin 전용 fetch는 반드시 `credentials: 'same-origin'` 포함

### GCP 인증 (`gcp/gcp.py`)
```
1순위: GOOGLE_APPLICATION_CREDENTIALS (서비스 계정 JSON)
       → gcp/service_account.json 저장 시 자동 설정
2순위: gcloud 토큰 폴백 (gcloud auth print-access-token)
       → 50분 TTL 캐시 (_creds_cache, _creds_cache_ts)
```
- 웹 설정 탭에서 서비스 계정 JSON 또는 Access Token 직접 입력 가능
- 캐시 무효화: `_gcp_mod._creds_cache = None`

### AWS 인증
- Management 계정 Access Key로 직접 인증
- 멤버 계정: STS AssumeRole → `AWSManagerReadOnlyRole`
- 웹 설정 탭에서 키 변경 가능 → `.env` 저장 + 즉시 반영

---

## API 엔드포인트 구조

### GCP (`/api/gcp/*`)
| 경로 | 설명 |
|---|---|
| `GET /api/gcp/status` | 프로젝트 목록 + 스캔 상태 |
| `POST /api/gcp/scan` | 전체 스캔 시작 |
| `GET /api/gcp/scan/stream` | SSE 진행률 |
| `GET /api/gcp/export` | Excel 다운로드 |
| `POST /api/gcp/projects/delete` | 프로젝트 삭제 (Admin) |
| `POST /api/gcp/resources/scan` | 리소스 스캔 |
| `GET /api/gcp/resources/stream` | SSE 진행률 |
| `GET /api/gcp/resources` | 리소스 결과 |
| `GET/POST /api/gcp/billing/settings` | BigQuery 설정 |
| `POST /api/gcp/billing/scan` | 빌링 비용 스캔 |
| `GET /api/gcp/billing/costs` | 빌링 비용 결과 |

### AWS (`/api/aws/*`)
| 경로 | 설명 |
|---|---|
| `GET /api/aws/accounts` | 계정 목록 (캐시 5분) |
| `GET /api/aws/costs/summary` | 이번달 비용 요약 (캐시) |
| `GET /api/aws/costs/{account_id}` | 계정별 월별 상세 |
| `GET /api/aws/cmdb/summary` | CMDB 요약 (5분 캐시) |
| `GET /api/aws/cmdb/account/{id}` | 계정 CMDB 상세 |
| `POST /api/aws/cmdb/collect` | CMDB 수집 트리거 (Admin) |
| `GET /api/aws/cmdb/collect/{cmd_id}` | 수집 상태 폴링 |
| `POST /api/aws/cmdb/refresh` | CMDB 캐시 갱신 |
| `GET /api/aws/org/tree` | OU 트리 (캐시) |
| `GET /api/aws/org/ous` | OU 평면 목록 (캐시) |
| `POST /api/aws/org/move` | 계정 이동 (Admin) |
| `POST /api/aws/cache/refresh` | AWS 전체 캐시 초기화 (Admin) |

### 공통
| 경로 | 설명 |
|---|---|
| `GET /api/overview` | 대시보드 GCP+AWS 요약 |
| `GET /health` | 서버 상태 확인 |
| `GET/POST /api/admin/credentials` | 인증 정보 조회/저장 |
| `POST /api/admin/credentials/aws` | AWS 키 저장 |
| `POST /api/admin/credentials/gcp/service_account` | GCP SA 저장 |
| `POST /api/admin/credentials/gcp/token` | GCP 토큰 주입 |
| `POST /api/admin/change-password` | 비밀번호 변경 |

---

## 프론트엔드 (`static/index.html`)

### 탭 구성 (사이드바)
```
🏠 대시보드
● GCP
  📋 프로젝트        → loadGcpProjects()
  🗂 리소스          → loadGcpResources()  (CMDB 스타일, 14종)
  💳 빌링 계정 관리  → loadGcpBilling()
● AWS
  🏢 계정            → loadAwsAccounts()
  🖥 리소스          → loadAwsCmdb()
  💰 빌링 현황       → loadAwsCosts()      (월별 탭 6개)
  🌐 조직 관리       → loadAwsOrg()
⚙ 설정              → loadSettings()       (Admin 전용)
```

### 캐시 전략
- **프론트 캐시**: `_awsFrontCache` (accounts, costs, orgTree, orgOus)
- **백엔드 캐시**: `_aws_cache` (TTL 5분)
- **탭 전환 시**: 캐시 즉시 표시 → 백그라운드 자동 갱신
- **중복 방지**: `_awsFetching` 진행 중이면 추가 요청 없음
- **강제 갱신**: 🔄 새로 수집 버튼

### 방치 프로젝트 판단 (`abandonedLevel`)
- score≥5 → 방치 가능성 높음 / score≥3 → 방치 의심
- OPEN 빌링 연결 시 0점 (정상 사용 중)
- 소유자 없음 +2 / 1·2·4년 초과 각 +1 / 기본명 +1 / test·temp 키워드 +1 / CLOSED 빌링 +1

---

## 주요 제약사항

1. **Python 3.9 호환** (EC2): `list[dict]` → `List[dict]`, `X | None` → `Optional[X]`
2. **boto3 전용**: aioboto3 사용 불가 (의존성 충돌)
3. **GCP 리소스 스캔**: `max_workers=5` 유지 (70 동시 연결 상한, 초과 시 쿼터 오류)
4. **GCP gRPC retry**: `retry=None, timeout=15` 필수
5. **CE API 리전**: `us-east-1` 필수
6. **SP 비용 필터**: `SP_RI_EXCLUDE_TYPES` 적용 필수
7. **Admin fetch**: 반드시 `credentials: 'same-origin'` 포함

---

## GCP 캐시 파일 (EC2 홈 디렉토리)
```
~/.msp_gcp_audit_cache.json       # 전체 프로젝트 스캔 결과
~/.msp_gcp_resource_cache.json    # 리소스 스캔 결과
~/.msp_gcp_billing_costs.json     # 빌링 비용 결과
```
> 현재 EC2에 캐시 없으면 gcp-audit 캐시(`~/.gcp_audit_cache.json`) 사용 중

---

## AWS 계정 구성
```
Organization: o-kxb3ey01jq
Management: kepbill_aws2 (221481233822)

멤버 계정 (17개):
  kep-sre (059780172050)          Infrastructure
  kep-shared (838155214946)       Infrastructure
  kep-sb-dng (533267064847)       Infrastructure
  kep-sb-kicserv (955637844268)   Infrastructure
  kep-sb-kicrd (257394469059)     Sandbox/Compliant
  kep-sb-cng (444212083352)       Sandbox/Compliant
  kep-sb-ing (614782867492)       Sandbox/Compliant
  kep-kicserv (685434951610)      Unmanaged
  kep-sec (775195110844)          Unmanaged
  kep-um-cse (970547334319)       Unmanaged
  kep-laas (616813723041)         PendingReview
  laasdev (829033947139)          PendingReview
  laasprod (869822862791)         PendingReview
  ConnectLive (846191957521)      DKT
  CNS_SPA_117 (512742310938)      Root (SP 구매 계정 — CE 비용 왜곡 주의)
  kep-playground (795913841331)   PolicyTest
  kepbill_aws2 (221481233822)     Root (Management)
```

---

## 알려진 이슈 / 해결된 버그

| 날짜 | 내용 |
|---|---|
| 2026-06-04 | Admin fetch에 `credentials: 'same-origin'` 누락 → CMDB 수집 등 403 오류 |
| 2026-06-04 | billing_open 빈값 처리 — `''` 를 CLOSED로 잘못 집계하던 버그 수정 |
| 2026-06-02 | GCP 리소스 스캔 전부 0 반환 — `max_workers=40`→`5` 수정 |
| 2026-06-02 | EC2 `.env` 미배포로 AWS API 인증 실패 — 수동 배포로 해결 |
| 이전 | _TokenBucketLimiter starvation — while True 루프 제거 |
| 이전 | CLOSED 빌링 계정 "미연결"로 표시 — billing_account_id 존재 여부로 판단 |
