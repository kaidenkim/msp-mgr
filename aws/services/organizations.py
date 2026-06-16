from typing import Optional
from aws.services.aws_session import get_management_session


def list_accounts(status_filter: Optional[str] = "ACTIVE"):
    """Organizations의 모든 멤버 계정 조회"""
    session = get_management_session()
    org = session.client("organizations")

    accounts = []
    paginator = org.get_paginator("list_accounts")
    for page in paginator.paginate():
        for acc in page["Accounts"]:
            if status_filter and acc["Status"] != status_filter:
                continue
            accounts.append({
                "id": acc["Id"],
                "name": acc["Name"],
                "email": acc["Email"],
                "status": acc["Status"],
                "joined_method": acc["JoinedMethod"],
                "joined_timestamp": acc["JoinedTimestamp"].isoformat(),
            })

    return accounts


def get_account_tags(account_id: str) -> dict:
    session = get_management_session()
    org = session.client("organizations")
    try:
        resp = org.list_tags_for_resource(ResourceId=account_id)
        return {t["Key"]: t["Value"] for t in resp.get("Tags", [])}
    except Exception:
        return {}
