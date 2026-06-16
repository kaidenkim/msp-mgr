import boto3
from config import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    AWS_DEFAULT_REGION,
    CROSS_ACCOUNT_ROLE_NAME,
)


def get_management_session(region: str = AWS_DEFAULT_REGION) -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        region_name=region,
    )


def get_account_session(account_id: str, region: str = AWS_DEFAULT_REGION) -> boto3.Session:
    """STS AssumeRole로 멤버 계정 세션 획득"""
    mgmt = get_management_session()
    sts = mgmt.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}"

    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="AWSManagerSession",
        DurationSeconds=3600,
    )
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
