# MSP Manager

GCP(kakaoenterprise.com 조직, ~780개 프로젝트)와 AWS(Organizations, 17개 계정)를 단일 웹 UI로 통합 관리하는 MSP 내부 운영 도구.

## 주요 기능

| 영역 | 기능 |
|---|---|
| GCP 프로젝트 | 빌링 상태·삭제 가능 여부 조회, 방치 프로젝트 판단, 체크박스 일괄 삭제 |
| GCP 리소스 | 빌링 연결 프로젝트 대상 14종 리소스 CMDB (VM, Cloud Run, GKE, Storage 등) |
| GCP 빌링 | 빌링 계정 연결 현황, BigQuery Export 기반 비용 조회 |
| AWS 계정 | Organizations 계정 목록 + 당월 비용 |
| AWS CMDB | EC2/S3/RDS/ECS/EKS/Subnet 탭별 조회 |
| AWS 비용 | 전체 요약 + 계정별 월별 서비스별 상세 |
| AWS 조직 | OU 트리 시각화, 계정 이동 |
| 일별 히스토리 | GCP/AWS 리소스 매일 자동 수집, 날짜별 과거 데이터 조회 |
| Admin 권한 | 프로젝트 삭제, 계정 이동, 설정 변경 (Viewer는 조회만) |

## 기술 스택

- **백엔드**: Python 3.9, FastAPI, uvicorn
- **프론트엔드**: 바닐라 JavaScript SPA (`static/index.html`)
- **GCP SDK**: google-cloud-resource-manager, google-cloud-billing, google-cloud-bigquery
- **AWS SDK**: boto3 (Organizations, Cost Explorer, SSM)
- **인프라**: EC2 `i-0a7fc7068c3acbf73` (ap-northeast-2), 포트 9070

## 파일 구조

```
msp-manager/
├── main.py              # FastAPI 엔드포인트 (~38개)
├── server.py            # uvicorn 진입점
├── auth.py              # Admin/Viewer 세션 인증
├── config.py            # 환경변수
├── deploy.sh            # S3 + SSM 배포 스크립트
├── gcp/
│   ├── gcp.py           # GCP API (프로젝트·빌링·IAM·리소스 스캔)
│   ├── billing.py       # GCP 빌링 비용 (BigQuery / REST)
│   └── export.py        # Excel 내보내기
├── aws/services/
│   ├── aws_session.py   # boto3 Management + AssumeRole
│   ├── organizations.py # 계정 목록
│   ├── cost_explorer.py # Cost Explorer (SP 필터 포함)
│   ├── cmdb.py          # CMDB 조회 + SSM 수집 트리거
│   └── resource_collector.py
└── static/index.html    # SPA (~3000줄)
```

## 설치 및 실행

### 요구사항

```bash
pip install -r requirements.txt
```

### 환경변수 설정

`.env.example`을 복사해 `.env` 작성:

```bash
cp .env.example .env
# .env 편집: AWS 키, GCP 인증, CMDB URL 등 입력
```

### 로컬 실행

```bash
python3 server.py
# → http://localhost:9070
```

### EC2 배포

```bash
# 단일 파일 배포 예시 (main.py)
aws s3 cp main.py s3://kep-sre-config/msp-manager/main.py --region ap-northeast-2
MAIN_URL=$(aws s3 presign s3://kep-sre-config/msp-manager/main.py --expires-in 600 --region ap-northeast-2)
aws ssm send-command \
  --instance-ids "i-0a7fc7068c3acbf73" \
  --document-name "AWS-RunShellScript" \
  --region ap-northeast-2 \
  --parameters "{\"commands\":[\"curl -s -o /home/ec2-user/msp-manager/main.py '${MAIN_URL}' && echo done\"]}"
```

> EC2에서 `aws s3 cp` 불가 (cryptography 모듈 오류) → presigned URL + curl 방식 사용

## 인증

| 역할 | 접근 방법 | 가능한 기능 |
|---|---|---|
| Viewer | 로그인 없이 직접 접속 | GCP/AWS 전체 조회 |
| Admin | `/login` → ID: `admin` / 초기 PW: `1234` | 삭제·이동·설정 변경 추가 |

Admin 세션은 서명된 쿠키로 관리 (8시간 유효).

## 일별 자동 수집

EC2 cron (매일 02:00 KST):

```
0 17 * * * /home/ec2-user/msp-manager/daily_collect.sh
```

수집 순서: GCP 프로젝트 스캔 → GCP 리소스 스캔 → AWS CMDB 갱신  
히스토리 저장 위치: `~/.msp_history/{kind}_{YYYY-MM-DD}.json` (90일 보관)

## 주요 제약사항

- Python **3.9** 호환 필수 (EC2 기준) — `list[dict]` 대신 `List[dict]` 사용
- `aioboto3` 사용 불가 (의존성 충돌) — 동기 boto3만 사용
- GCP 리소스 스캔 `max_workers=5` 유지 (쿼터 초과 방지)
- Cost Explorer API는 반드시 `us-east-1` 리전 호출
- Admin 전용 fetch는 반드시 `credentials: 'same-origin'` 포함

## EC2 접속

```bash
# 세션 접속
aws ssm start-session --target i-0a7fc7068c3acbf73

# 포트포워딩 (로컬에서 접속)
aws ssm start-session --target i-0a7fc7068c3acbf73 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["9070"],"localPortNumber":["9070"]}'
# → http://localhost:9070
```

---

내부 개발 가이드는 [CLAUDE.md](CLAUDE.md) 및 [PROMPT.md](PROMPT.md) 참조.
