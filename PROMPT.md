# MSP Manager — 통합 클라우드 관리 플랫폼 개발 프롬프트

## 1. 프로젝트 개요

GCP(kakaoenterprise.com 조직, ~780개 프로젝트)와 AWS(Organizations 기반 17개 멤버 계정)를
**단일 웹 UI**에서 통합 관리하는 MSP 내부 운영 도구.

- **소스 경로**: `/Users/kaiden.kim/msp-manager/` (로컬 Mac), `/home/ec2-user/msp-manager/` (EC2)
- **서버 포트**: 9070 (uvicorn + FastAPI)
- **EC2 인스턴스**: `i-0a7fc7068c3acbf73` (ap-northeast-2)
- **배포용 S3 버킷**: `kep-sre-config` (경로: `msp-manager/`)
- **EC2 접속**: `aws ssm start-session --target i-0a7fc7068c3acbf73`

### 통합 대상 클라우드

| 클라우드 | 범위 | 출처 프로젝트 |
|---|---|---|
| GCP | kakaoenterprise.com 조직, 프로젝트 ~780개, 빌링 연결 148개 | gcp-audit |
| AWS | Organizations o-kxb3ey01jq, Management 221481233822, 멤버 17개 | aws-manager |

---

## 2. 파일 구조

```
msp-manager/
├── main.py                    # FastAPI 앱 진입점, 라우터 통합, lifespan
├── auth.py                    # 인증 모듈 (Viewer/Admin, 쿠키 세션)
├── config.py                  # 환경변수 (dotenv)
├── server.py                  # uvicorn 직접 실행 진입점
├── deploy.sh                  # EC2 SSM 배포 스크립트
├── requirements.txt
├── .env                       # 실 자격증명 (git 제외)
├── .env.example
│
├── gcp/
│   ├── gcp.py                 # GCP API 호출 (프로젝트, 빌링, IAM, 리소스 스캔)
│   ├── billing.py             # GCP 빌링 비용 조회 (BigQuery Export / REST)
│   └── export.py              # GCP 스캔 결과 Excel 내보내기
│
├── aws/
│   ├── session.py             # boto3 세션 관리 (Management + AssumeRole)
│   ├── organizations.py       # Organizations 계정 목록, OU 트리
│   ├── resources.py           # EC2/VPC/RDS/Lambda 수집
│   ├── costs.py               # Cost Explorer 비용 조회
│   └── cmdb.py                # CMDB 데이터 조회 및 수집 트리거
│
├── routers/
│   ├── gcp_projects.py        # /api/gcp/projects/*
│   ├── gcp_resources.py       # /api/gcp/resources/*
│   ├── gcp_billing.py         # /api/gcp/billing/*
│   ├── aws_accounts.py        # /api/aws/accounts
│   ├── aws_resources.py       # /api/aws/resources/*
│   ├── aws_costs.py           # /api/aws/costs/*
│   ├── aws_cmdb.py            # /api/aws/cmdb/*
│   └── aws_org.py             # /api/aws/org/*
│
└── static/
    └── index.html             # 단일 페이지 앱 (SPA, 바닐라 JS)

캐시 파일 (홈 디렉토리):
  ~/.msp_gcp_audit_cache.json          — GCP 전체 프로젝트 스캔 결과
  ~/.msp_gcp_resource_cache.json       — GCP 리소스 스캔 결과
  ~/.msp_gcp_billing_costs.json        — GCP 비용 스캔 결과
  ~/.msp_gcp_billing_settings.json     — GCP BigQuery 설정
```

---

## 3. 기술 스택

### Python 백엔드

| 라이브러리 | 버전 | 용도 |
|---|---|---|
| `fastapi` | 0.115.0 | REST API + SSE 스트리밍 |
| `uvicorn[standard]` | 0.30.6 | ASGI 서버 |
| `boto3` | 최신 | AWS SDK (aioboto3 사용 불가 — 의존성 충돌) |
| `google-cloud-bigquery` | 3.25.0 | GCP 빌링 비용 조회 |
| `google-cloud-resource-manager` | 최신 | GCP 프로젝트 목록/IAM |
| `google-cloud-billing` | 최신 | GCP 빌링 계정 조회 |
| `google-auth` | (간접) | GCP 인증 |
| `openpyxl` | 3.1.5 | Excel 내보내기 |
| `itsdangerous` | 최신 | 세션 쿠키 서명 |
| `python-multipart` | 최신 | Form 파싱 (로그인) |
| `python-dotenv` | 최신 | 환경변수 |

**Python 버전 제약**: EC2 Python 3.9 호환 필수
- `list[dict]`, `dict | None` 등 3.10+ 타입힌트 사용 금지
- `Optional[X]`, `List[X]` 사용

### 프론트엔드

- 순수 바닐라 JavaScript (프레임워크 없음)
- `fetch()` API + `EventSource` (SSE)
- CSS 인라인 `<style>` 태그
- Jinja2 템플릿 (role 변수 주입)

---

## 4. 인증

### Viewer (익명)
- 로그인 없이 직접 접속 가능
- GCP/AWS 조회 기능 전체 사용 가능
- Admin 전용 액션(프로젝트 삭제, 계정 이동) 버튼 미표시

### Admin (로그인 필요)
- 헤더 "관리자 로그인" → `/login`
- ID: `admin`, 초기 비밀번호: `1234`
- 로그인 성공 시 서명된 쿠키 발급 (8시간)
- Admin 전용 기능: GCP 프로젝트 삭제, AWS 계정 OU 이동, 설정 탭 비밀번호 변경

### 구현 (`auth.py`)
```python
import hashlib
from itsdangerous import URLSafeTimedSerializer

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# 저장: /app/msp-manager/data/admin_credentials.json
# 형식: {"password_hash": "sha256_hex_string"}

def make_session() -> str:
    return _signer.dumps({"role": "admin"})

def require_admin(request: Request) -> dict:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="관리자 로그인이 필요합니다")
    return {"role": "admin"}
```

### Admin 전용 엔드포인트
- `POST /api/gcp/projects/delete`
- `POST /api/aws/org/move`
- `POST /api/admin/change-password`

---

## 5. GCP 연동 (`gcp/gcp.py`)

gcp-audit 프로젝트의 구현을 그대로 이식. 핵심 사항 요약:

### 인증
```python
# 1순위: ADC (Application Default Credentials)
# 2순위: gcloud 토큰 폴백 (gcloud auth print-access-token)
# → OAuthCreds(token=...) 50분 TTL 캐싱
```

### gcloud 바이너리 탐색 경로
```python
_GCLOUD_CANDIDATES = [
    "gcloud", "/usr/bin/gcloud", "/usr/local/bin/gcloud",
    "/usr/local/google-cloud-sdk/bin/gcloud",
    "/opt/google-cloud-sdk/bin/gcloud",
    "/opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin/gcloud",
    "/home/ec2-user/google-cloud-sdk/bin/gcloud",
    "/root/google-cloud-sdk/bin/gcloud",
]
```

### 전체 스캔 (`full_scan`) — 4단계
1. 인증 초기화 + gRPC 클라이언트 풀 생성 + 빌링 계정 백그라운드 조회
2. `search_projects(page_size=1000)` 스트리밍 → billing_task + owner_task 병렬 제출 (max_workers=100)
3. 빌링 계정 결과 수집 + 누락 계정 OPEN/CLOSED 보완
4. billing_failed + owner_failed 프로젝트 재시도

### API 할당량 제어
```python
_BILLING_LIM = _TokenBucketLimiter(650)   # Cloud Billing API: 700/min 쿼터
_OWNER_LIM   = _TokenBucketLimiter(570)   # Resource Manager IAM: 600/min 쿼터
```

### 리소스 스캔 (`scan_billing_resources`)
- 빌링 연결 프로젝트만 대상 (148개)
- max_workers=5 (70 동시 HTTP 연결 상한)
- 14개 리소스 타입: VM, Run, Functions, GKE, Storage, SQL, PubSub, VPC, LB, Armor, SA, LogSink, LogBucket, Marketplace

### 삭제 가능 여부 판단
| 값 | 조건 |
|---|---|
| `"빌링연결"` | billing_enabled=True OR billing_account_id 존재 |
| `"소유자 확인 필요"` | 빌링 미연결 + 사람 소유자 없음 + 비기본 프로젝트명 |
| `"즉시 삭제 가능"` | 빌링 미연결 + 사람 소유자 있음 OR 기본 프로젝트명 |

---

## 6. AWS 연동 (`aws/`)

aws-manager 프로젝트의 구현을 그대로 이식.

### 세션 관리 (`aws/session.py`)
```python
def get_management_session(region="ap-northeast-2"):
    return boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=region,
    )

def get_account_session(account_id: str, region="ap-northeast-2"):
    sts = get_management_session().client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"
    resp = sts.assume_role(RoleArn=role_arn, RoleSessionName="MSPManagerSession")
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
```

### AWS 계정 구성
```
Organization ID: o-kxb3ey01jq
Management: kepbill_aws2 (221481233822)
멤버 계정 17개:
  kep-sre (059780172050), kep-sb-dng (533267064847), kep-shared (838155214946)
  kep-playground (795913841331), kep-sb-kicrd (257394469059), kep-sb-cng (444212083352)
  kep-sb-ing (614782867492), kep-kicserv (685434951610), kep-sec (775195110844)
  kep-um-cse (970547334319), kep-laas (616813723041), laasdev (829033947139)
  laasprod (869822862791), ConnectLive (846191957521), CNS_SPA_117 (512742310938)
  kep-sb-kicserv (955637844268), kepbill_aws2 (221481233822)
```

### 비용 조회 주의사항
- CE API는 반드시 `us-east-1` 리전에서 호출
- SP 비용 왜곡 방지: `RECORD_TYPE` 필터 적용
```python
SP_RI_EXCLUDE_TYPES = [
    "SavingsPlanRecurringFee", "SavingsPlanNegation", "SavingsPlanUpfrontFee",
    "RIFee", "Enterprise Discount Program Discount", "Tax", "Credit", "Refund"
]
```

### CMDB 데이터 소스
```
URL:  https://dxvqjzd9yg287.cloudfront.net/cmdb_results.json
Auth: Basic (collector / <실제값은 .env 참조>)
수집 Role: stacksets-csre-readonly
수집 리전: ap-northeast-2, ap-northeast-1, us-east-1
```

---

## 7. API 설계

### GCP 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/gcp/status` | GCP 스캔 상태 + 프로젝트 목록 |
| POST | `/api/gcp/scan` | GCP 전체 스캔 시작 |
| GET | `/api/gcp/scan/stream` | GCP 스캔 진행률 SSE |
| GET | `/api/gcp/export` | Excel 다운로드 |
| POST | `/api/gcp/projects/delete` | 프로젝트 삭제 (Admin) |
| POST | `/api/gcp/resources/scan` | GCP 리소스 스캔 시작 |
| GET | `/api/gcp/resources/stream` | 리소스 스캔 SSE |
| GET | `/api/gcp/resources` | 리소스 스캔 결과 |
| GET | `/api/gcp/billing/settings` | BigQuery 설정 조회 |
| POST | `/api/gcp/billing/settings` | BigQuery 설정 저장 |
| POST | `/api/gcp/billing/scan` | GCP 비용 스캔 시작 |
| GET | `/api/gcp/billing/stream` | GCP 비용 스캔 SSE |
| GET | `/api/gcp/billing` | GCP 비용 스캔 결과 |

### AWS 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/aws/accounts` | Organizations 계정 목록 + 이번달 비용 |
| GET | `/api/aws/resources/{account_id}` | 계정별 리소스 수집 |
| GET | `/api/aws/costs/summary` | 전체 계정 월별 비용 요약 |
| GET | `/api/aws/costs/{account_id}` | 계정별 서비스별 비용 상세 |
| GET | `/api/aws/cmdb/summary` | CMDB 계정별 요약 (5분 캐시) |
| GET | `/api/aws/cmdb/account/{account_id}` | CMDB 계정 상세 |
| POST | `/api/aws/cmdb/collect` | CMDB 수집 트리거 |
| GET | `/api/aws/cmdb/collect/{command_id}` | 수집 진행 상태 |
| POST | `/api/aws/cmdb/refresh` | CMDB 캐시 갱신 |
| GET | `/api/aws/org/tree` | OU 트리 조회 |
| GET | `/api/aws/org/ous` | OU 평면 목록 |
| POST | `/api/aws/org/move` | 계정 OU 이동 (Admin) |

### 공통 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/login` | Admin 로그인 |
| GET | `/logout` | 로그아웃 |
| POST | `/api/admin/change-password` | 비밀번호 변경 (Admin) |
| GET | `/api/overview` | GCP + AWS 현황 요약 (대시보드용) |

### SSE 스트리밍 패턴 (GCP 스캔)
```python
yield f"data: {json.dumps({'type': 'progress', 'pct': 50, 'stage': '조회 중...'})}\n\n"
yield f"data: {json.dumps({'type': 'done'})}\n\n"
yield f"data: {json.dumps({'type': 'error', 'message': '...'})}\n\n"
```

---

## 8. 프론트엔드 (`static/index.html`)

### 탭 구성

| 탭 이름 | 내용 | 출처 |
|---|---|---|
| **대시보드** | GCP+AWS 통합 현황 카드 (프로젝트수, 계정수, 총 리소스, 총 비용) | 신규 |
| **GCP 프로젝트** | 빌링 상태·삭제 가능 여부 필터, 프로젝트 목록, 삭제(Admin) | gcp-audit |
| **GCP 리소스** | 빌링 연결 프로젝트별 14종 리소스 현황 | gcp-audit |
| **GCP 빌링** | 빌링 계정 현황 / BigQuery 비용 조회 | gcp-audit |
| **AWS 계정** | Organizations 전체 계정 + 이번달 비용 | aws-manager |
| **AWS CMDB** | EC2/S3/RDS/ECS/EKS/Subnet 탭별 조회 | aws-manager |
| **AWS 비용** | 전체 요약 + 계정별 월별 서비스별 | aws-manager |
| **AWS 조직** | OU 트리, 계정 이동(Admin) | aws-manager |
| **설정** | Admin: 비밀번호 변경, GCP BigQuery 설정 | 통합 |

### 전역 상태 변수
```javascript
const USER_ROLE = "{{ role }}";   // "admin" | "viewer"

// GCP
let GCP_PROJECTS = [];
let GCP_RESOURCES = [];
let GCP_BILLING = [];

// AWS
let AWS_ACCOUNTS = [];
let AWS_CMDB = {};
let AWS_COSTS = {};
```

### 대시보드 요약 카드 (`/api/overview`)
```javascript
// 응답 예시
{
  "gcp": {
    "total_projects": 780,
    "billing_connected": 148,
    "total_resources": 833,
    "deletable_immediately": 45
  },
  "aws": {
    "total_accounts": 17,
    "total_ec2": 120,
    "total_rds": 30,
    "current_month_cost_usd": 539.92
  }
}
```

### GCP 빌링 상태 표시 로직
```javascript
if (p.billing_enabled === 'True') {
  const openTag = p.billing_open === 'True' ? '● OPEN' : p.billing_open === 'False' ? '● CLOSED' : '';
  bSt = `연결됨 ${openTag}`;
} else if (p.billing_account_id) {
  bSt = '연결됨 ● CLOSED';
} else {
  bSt = '미연결';
}
```

### GCP 리소스 컬럼 순서
`🖥VM | ☁Run | ⚡Fn | ⎈GKE | 🗄Stor | 🗃SQL | 📨PS | 🌐VPC | ⚖LB | 🛡Armor | 👤SA | 📋Sink | 📦Bkt | 🏪Mkt`

### Admin 조건부 렌더링
```javascript
if (USER_ROLE === 'admin') {
  // GCP: 삭제 버튼 표시
  // AWS: 계정 이동 버튼 표시
  // 설정 탭 표시
}
```

---

## 9. 환경변수 (`config.py` / `.env`)

```env
# AWS Management 계정
AWS_ACCESS_KEY_ID=<실제값은 .env 참조>
AWS_SECRET_ACCESS_KEY=<실제값은 .env 참조>
AWS_DEFAULT_REGION=ap-northeast-2
MANAGEMENT_ACCOUNT_ID=221481233822
CROSS_ACCOUNT_ROLE_NAME=AWSManagerReadOnlyRole

# Admin 세션 서명 키
SECRET_KEY=msp-manager-secret-key-change-me

# CMDB
CMDB_URL=https://dxvqjzd9yg287.cloudfront.net/cmdb_results.json
CMDB_USER=collector
CMDB_PASS=<실제값은 .env 참조>
CMDB_COLLECTOR_INSTANCE=i-0a7fc7068c3acbf73
CMDB_COLLECTOR_PATH=/root/jobs/cmdb-collect

# GCP (선택 — ADC 미사용 시 gcloud 폴백)
# GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/application_default_credentials.json
```

---

## 10. 서버 시작 (lifespan)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _load_gcp_cache()           # GCP 캐시 파일 복원
    threading.Thread(target=_refresh_auth_cache, daemon=True).start()  # 60s마다 gcloud 계정 갱신
    threading.Thread(target=_warmup_gcp_credentials, daemon=True).start()
    yield
```

---

## 11. 배포 절차

### 로컬 → S3 업로드
```bash
aws s3 cp gcp/gcp.py s3://kep-sre-config/msp-manager/gcp/gcp.py --region ap-northeast-2
aws s3 cp static/index.html s3://kep-sre-config/msp-manager/index.html --region ap-northeast-2
# ... 기타 파일
```

### presigned URL 생성 후 SSM 배포
```bash
# presigned URL 생성 (600초 유효)
GCP_URL=$(aws s3 presign s3://kep-sre-config/msp-manager/gcp/gcp.py --expires-in 600 --region ap-northeast-2)

# SSM으로 EC2 배포 + 재시작
aws ssm send-command \
  --instance-ids "i-0a7fc7068c3acbf73" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "{\"commands\":[
    \"cd /home/ec2-user/msp-manager\",
    \"curl -s -o gcp/gcp.py '${GCP_URL}'\",
    \"lsof -ti :9070 | xargs kill -9 2>/dev/null || true\",
    \"sleep 2\",
    \"nohup python3 server.py >> /tmp/msp-manager.log 2>&1 &\",
    \"sleep 4\",
    \"curl -s http://localhost:9070/api/overview | python3 -c \\\"import sys,json; print(json.load(sys.stdin))\\\"\"
  ]}"
```

**주의**: EC2에서 `aws s3 cp`는 `No module named 'cryptography'` 오류 발생 → 반드시 **curl + presigned GET URL** 방식 사용.

### EC2 포트포워딩 접속
```bash
aws ssm start-session --target i-0a7fc7068c3acbf73 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9070"],"localPortNumber":["9070"]}'
# → http://localhost:9070
```

---

## 12. 개발 시 주의사항

1. **Python 3.9 호환**: 3.10+ 타입힌트 사용 금지 (`list[dict]` → `List[dict]`, `X | None` → `Optional[X]`)

2. **boto3 전용**: `aioboto3` 의존성 충돌로 사용 불가. 동기 boto3만 사용.

3. **GCP gRPC billing_v1 호환 불가**: EC2 환경에서 `ACCESS_TOKEN_TYPE_UNSUPPORTED` 오류 → 빌링 API는 모두 REST (`AuthorizedSession`) 사용.

4. **GCP 리소스 스캔 max_workers**: 반드시 5 이하 유지. `max_workers=40` 시 560 동시 연결 → 쿼터 초과 → 전부 0 반환.

5. **CE API 리전**: Cost Explorer는 반드시 `us-east-1` 호출.

6. **SP 비용 왜곡**: CE API `LINKED_ACCOUNT` 집계 시 SP 구매 계정에 전액 귀속 → `RECORD_TYPE` 필터 필수.

7. **GCP gRPC retry**: `retry=None, timeout=15` 필수 (기본값은 최대 600초 재시도).

8. **로컬 개발**: GCP는 로컬에서도 서버 기동 가능 (`python3 server.py`). AWS는 EC2 전용 (자격증명은 `.env` 관리).

---

## 13. 로컬 개발 실행

```bash
cd /Users/kaiden.kim/msp-manager
python3 server.py
# → http://localhost:9070

# GCP 캐시 초기화 후 재시작
rm -f ~/.msp_gcp_*.json
python3 server.py
```

---

## 14. 현재 기준 데이터 규모 (2026-06-02)

### GCP
- 전체 프로젝트: 780개
- 빌링 연결: 148개
- 총 리소스: 833개 (log_sink 298, log_bucket 296, sa 105, storage 59, vpc 28 등)
- 스캔 소요: 전체 ~2분, 리소스 ~45초

### AWS
- 총 계정: 17개 (Management 포함)
- 수집 리전: ap-northeast-2 (서울), ap-northeast-1 (도쿄), us-east-1 (버지니아)
- 이번달 총 비용: ~$539.92 (SP 수수료 필터 후)
