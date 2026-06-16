import os
from pathlib import Path
from dotenv import load_dotenv

# 절대 경로로 .env 로드 (작업 디렉토리와 무관하게)
_ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE, override=True)

# AWS
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "ap-northeast-2")
CROSS_ACCOUNT_ROLE_NAME = os.getenv("CROSS_ACCOUNT_ROLE_NAME", "AWSManagerReadOnlyRole")
MANAGEMENT_ACCOUNT_ID = os.getenv("MANAGEMENT_ACCOUNT_ID", "")
SECRET_KEY = os.getenv("SECRET_KEY", "msp-manager-secret-key")
CMDB_URL = os.getenv("CMDB_URL", "")
CMDB_USER = os.getenv("CMDB_USER", "")
CMDB_PASS = os.getenv("CMDB_PASS", "")
CMDB_COLLECTOR_INSTANCE = os.getenv("CMDB_COLLECTOR_INSTANCE", "")
CMDB_COLLECTOR_PATH = os.getenv("CMDB_COLLECTOR_PATH", "/root/jobs/cmdb-collect")
RESOURCE_REGIONS = ["ap-northeast-2", "ap-northeast-1", "us-east-1"]
