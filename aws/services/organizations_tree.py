from aws.services.aws_session import get_management_session


def _org_client():
    return get_management_session(region="us-east-1").client("organizations")


def get_ou_tree() -> dict:
    org = _org_client()
    root = org.list_roots()["Roots"][0]

    def build_node(parent_id: str, name: str, node_id: str, is_root: bool = False) -> dict:
        node = {"id": node_id, "name": name, "type": "root" if is_root else "ou", "children": [], "accounts": []}

        # 하위 OU
        paginator = org.get_paginator("list_organizational_units_for_parent")
        for page in paginator.paginate(ParentId=parent_id):
            for ou in page["OrganizationalUnits"]:
                node["children"].append(build_node(ou["Id"], ou["Name"], ou["Id"]))

        # 직속 계정
        paginator = org.get_paginator("list_children")
        for page in paginator.paginate(ParentId=parent_id, ChildType="ACCOUNT"):
            for child in page["Children"]:
                try:
                    acc = org.describe_account(AccountId=child["Id"])["Account"]
                    node["accounts"].append({
                        "id": acc["Id"],
                        "name": acc["Name"],
                        "email": acc["Email"],
                        "status": acc["Status"],
                        "parent_id": parent_id,
                    })
                except Exception:
                    node["accounts"].append({"id": child["Id"], "name": child["Id"], "parent_id": parent_id})

        return node

    return build_node(root["Id"], "Root", root["Id"], is_root=True)


def get_all_ous_flat():
    """이동 대상 선택용 OU 평면 목록"""
    org = _org_client()
    root = org.list_roots()["Roots"][0]
    result = [{"id": root["Id"], "name": "Root", "path": "Root"}]

    def traverse(parent_id: str, path: str):
        paginator = org.get_paginator("list_organizational_units_for_parent")
        for page in paginator.paginate(ParentId=parent_id):
            for ou in page["OrganizationalUnits"]:
                full_path = f"{path} / {ou['Name']}"
                result.append({"id": ou["Id"], "name": ou["Name"], "path": full_path})
                traverse(ou["Id"], full_path)

    traverse(root["Id"], "Root")
    return result


def get_account_parent(account_id: str) -> dict:
    org = _org_client()
    parents = org.list_parents(ChildId=account_id)["Parents"]
    if not parents:
        return {}
    p = parents[0]
    if p["Type"] == "ROOT":
        root = org.list_roots()["Roots"][0]
        return {"id": root["Id"], "name": "Root", "type": "ROOT"}
    ou = org.describe_organizational_unit(OrganizationalUnitId=p["Id"])["OrganizationalUnit"]
    return {"id": ou["Id"], "name": ou["Name"], "type": "ORGANIZATIONAL_UNIT"}


def move_account(account_id: str, source_parent_id: str, dest_parent_id: str) -> dict:
    org = _org_client()
    org.move_account(
        AccountId=account_id,
        SourceParentId=source_parent_id,
        DestinationParentId=dest_parent_id,
    )
    return {"ok": True, "account_id": account_id, "from": source_parent_id, "to": dest_parent_id}
