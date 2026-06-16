import urllib.request
import urllib.error
import base64
import json
import time
from config import CMDB_URL, CMDB_USER, CMDB_PASS, CMDB_COLLECTOR_INSTANCE, CMDB_COLLECTOR_PATH
from aws.services.aws_session import get_management_session

_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5분


def fetch_cmdb_data(force: bool = False):
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    credentials = base64.b64encode(f"{CMDB_USER}:{CMDB_PASS}".encode()).decode()
    req = urllib.request.Request(
        CMDB_URL,
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    _cache["data"] = data
    _cache["ts"] = now
    return data


def get_cmdb_summary():
    data = fetch_cmdb_data()
    summary = []
    for acc in data:
        summary.append({
            "account_id": acc.get("project_id"),
            "account_name": acc.get("project"),
            "ec2": len(acc.get("EC2", [])),
            "s3": len(acc.get("S3", [])),
            "rds": len(acc.get("RDS", [])),
            "ecs": len(acc.get("ECS", [])),
            "eks": len(acc.get("EKS", [])),
            "subnet": len(acc.get("SUBNET", [])),
        })
    return summary


def get_cmdb_account(account_id: str) -> dict:
    data = fetch_cmdb_data()
    for acc in data:
        if acc.get("project_id") == account_id:
            return acc
    return None


def trigger_collection() -> str:
    """EC2에서 CMDB 수집 스크립트를 SSM으로 실행"""
    session = get_management_session()
    ssm = session.client("ssm", region_name="ap-northeast-2")

    resp = ssm.send_command(
        InstanceIds=[CMDB_COLLECTOR_INSTANCE],
        DocumentName="AWS-RunShellScript",
        Comment="cmdb-collect trigger",
        Parameters={
            "commands": [
                f"cd {CMDB_COLLECTOR_PATH}",
                "source /root/.bashrc 2>/dev/null || true",
                f"AWS_DEFAULT_REGION=ap-northeast-2 /usr/bin/python3 {CMDB_COLLECTOR_PATH}/main.py >> {CMDB_COLLECTOR_PATH}/cmdb.log 2>&1",
                "echo done",
            ]
        },
    )
    return resp["Command"]["CommandId"]


def get_collection_status(command_id: str) -> dict:
    session = get_management_session()
    ssm = session.client("ssm", region_name="ap-northeast-2")
    try:
        resp = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=CMDB_COLLECTOR_INSTANCE,
        )
        return {
            "status": resp["Status"],
            "stdout": resp.get("StandardOutputContent", ""),
            "stderr": resp.get("StandardErrorContent", ""),
        }
    except Exception as e:
        return {"status": "Pending", "stdout": "", "stderr": str(e)}
