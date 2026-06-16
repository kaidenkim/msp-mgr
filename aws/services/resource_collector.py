from concurrent.futures import ThreadPoolExecutor, as_completed
from aws.services.aws_session import get_account_session
from config import RESOURCE_REGIONS


def _collect_ec2(session, region: str, account_id: str):
    ec2 = session.client("ec2", region_name=region)
    instances = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for r in page["Reservations"]:
                for i in r["Instances"]:
                    name = next(
                        (t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), ""
                    )
                    instances.append({
                        "type": "EC2",
                        "id": i["InstanceId"],
                        "name": name,
                        "state": i["State"]["Name"],
                        "instance_type": i["InstanceType"],
                        "platform": i.get("Platform", "linux"),
                        "private_ip": i.get("PrivateIpAddress", ""),
                        "public_ip": i.get("PublicIpAddress", ""),
                        "launch_time": i["LaunchTime"].isoformat(),
                        "region": region,
                        "account_id": account_id,
                    })
    except Exception as e:
        print(f"[EC2] {account_id}/{region} error: {e}")
    return instances


def _collect_vpc(session, region: str, account_id: str):
    ec2 = session.client("ec2", region_name=region)
    resources = []
    try:
        # VPCs
        for vpc in ec2.describe_vpcs()["Vpcs"]:
            name = next((t["Value"] for t in vpc.get("Tags", []) if t["Key"] == "Name"), "")
            resources.append({
                "type": "VPC",
                "id": vpc["VpcId"],
                "name": name,
                "cidr": vpc["CidrBlock"],
                "is_default": vpc["IsDefault"],
                "state": vpc["State"],
                "region": region,
                "account_id": account_id,
            })
        # Subnets
        paginator = ec2.get_paginator("describe_subnets")
        for page in paginator.paginate():
            for s in page["Subnets"]:
                name = next((t["Value"] for t in s.get("Tags", []) if t["Key"] == "Name"), "")
                resources.append({
                    "type": "Subnet",
                    "id": s["SubnetId"],
                    "name": name,
                    "cidr": s["CidrBlock"],
                    "vpc_id": s["VpcId"],
                    "az": s["AvailabilityZone"],
                    "available_ips": s["AvailableIpAddressCount"],
                    "public": s["MapPublicIpOnLaunch"],
                    "region": region,
                    "account_id": account_id,
                })
        # Security Groups
        paginator = ec2.get_paginator("describe_security_groups")
        for page in paginator.paginate():
            for sg in page["SecurityGroups"]:
                resources.append({
                    "type": "SecurityGroup",
                    "id": sg["GroupId"],
                    "name": sg["GroupName"],
                    "description": sg["Description"],
                    "vpc_id": sg.get("VpcId", ""),
                    "region": region,
                    "account_id": account_id,
                })
    except Exception as e:
        print(f"[VPC] {account_id}/{region} error: {e}")
    return resources


def _collect_rds(session, region: str, account_id: str):
    rds = session.client("rds", region_name=region)
    resources = []
    try:
        paginator = rds.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page["DBInstances"]:
                resources.append({
                    "type": "RDS",
                    "id": db["DBInstanceIdentifier"],
                    "name": db["DBInstanceIdentifier"],
                    "engine": f"{db['Engine']} {db['EngineVersion']}",
                    "instance_class": db["DBInstanceClass"],
                    "status": db["DBInstanceStatus"],
                    "storage_gb": db["AllocatedStorage"],
                    "multi_az": db["MultiAZ"],
                    "endpoint": db.get("Endpoint", {}).get("Address", ""),
                    "region": region,
                    "account_id": account_id,
                })
        # Aurora clusters
        paginator = rds.get_paginator("describe_db_clusters")
        for page in paginator.paginate():
            for c in page["DBClusters"]:
                resources.append({
                    "type": "AuroraCluster",
                    "id": c["DBClusterIdentifier"],
                    "name": c["DBClusterIdentifier"],
                    "engine": f"{c['Engine']} {c['EngineVersion']}",
                    "instance_class": "",
                    "status": c["Status"],
                    "storage_gb": c.get("AllocatedStorage", 0),
                    "multi_az": c.get("MultiAZ", False),
                    "endpoint": c.get("Endpoint", ""),
                    "region": region,
                    "account_id": account_id,
                })
    except Exception as e:
        print(f"[RDS] {account_id}/{region} error: {e}")
    return resources


def _collect_lambda(session, region: str, account_id: str):
    lmb = session.client("lambda", region_name=region)
    resources = []
    try:
        paginator = lmb.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                resources.append({
                    "type": "Lambda",
                    "id": fn["FunctionName"],
                    "name": fn["FunctionName"],
                    "runtime": fn.get("Runtime", ""),
                    "memory_mb": fn["MemorySize"],
                    "timeout_sec": fn["Timeout"],
                    "last_modified": fn["LastModified"],
                    "code_size_bytes": fn["CodeSize"],
                    "region": region,
                    "account_id": account_id,
                })
    except Exception as e:
        print(f"[Lambda] {account_id}/{region} error: {e}")
    return resources


def collect_account_resources(account_id: str, regions: list = None) -> dict:
    """계정의 모든 리소스를 병렬로 수집"""
    if regions is None:
        regions = RESOURCE_REGIONS

    try:
        session = get_account_session(account_id)
    except Exception as e:
        return {"error": str(e), "account_id": account_id, "resources": []}

    all_resources = []

    def collect_region(region):
        results = []
        results.extend(_collect_ec2(session, region, account_id))
        results.extend(_collect_vpc(session, region, account_id))
        results.extend(_collect_rds(session, region, account_id))
        results.extend(_collect_lambda(session, region, account_id))
        return results

    with ThreadPoolExecutor(max_workers=len(regions)) as executor:
        futures = {executor.submit(collect_region, r): r for r in regions}
        for future in as_completed(futures):
            try:
                all_resources.extend(future.result())
            except Exception as e:
                print(f"Region collection error: {e}")

    summary = {}
    for r in all_resources:
        t = r["type"]
        summary[t] = summary.get(t, 0) + 1

    return {
        "account_id": account_id,
        "resources": all_resources,
        "summary": summary,
        "total": len(all_resources),
    }
