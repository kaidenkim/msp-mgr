from datetime import date, timedelta
from aws.services.aws_session import get_management_session

# SavingsPlanRecurringFee: SP 월 수수료 (실사용이 아닌 금융 항목)
# SavingsPlanNegation: SP 적용 상쇄 항목
# SavingsPlanUpfrontFee: SP 일시불 구매
# RIFee: Reserved Instance 수수료
# Enterprise Discount Program Discount: EDP 할인 (수수료 없으면 의미없어짐)
# Tax / Credit / Refund: 세금·크레딧·환불
SP_RI_EXCLUDE_TYPES = [
    "SavingsPlanRecurringFee",
    "SavingsPlanNegation",
    "SavingsPlanUpfrontFee",
    "RIFee",
    "Enterprise Discount Program Discount",
    "Tax",
    "Credit",
    "Refund",
]

# SP 수수료 항목만 별도 조회용
SP_FEE_TYPES = [
    "SavingsPlanRecurringFee",
    "SavingsPlanUpfrontFee",
]


def get_account_costs(
    account_id: str,
    start: str = None,
    end: str = None,
    granularity: str = "MONTHLY",
) -> dict:
    if not end:
        end = date.today().isoformat()
    if not start:
        start = (date.today().replace(day=1) - timedelta(days=180)).replace(day=1).isoformat()

    session = get_management_session()
    ce = session.client("ce", region_name="us-east-1")

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Filter={
                "And": [
                    {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
                    {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": SP_RI_EXCLUDE_TYPES}}},
                ]
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )

        # SP 수수료 별도 조회
        sp_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Filter={
                "And": [
                    {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
                    {"Dimensions": {"Key": "RECORD_TYPE", "Values": SP_FEE_TYPES}},
                ]
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )

        # SP 수수료 기간별 집계
        sp_fee_by_period = {}
        for result in sp_resp["ResultsByTime"]:
            period_start = result["TimePeriod"]["Start"]
            sp_total = sum(
                float(g["Metrics"]["UnblendedCost"]["Amount"])
                for g in result["Groups"]
            )
            sp_fee_by_period[period_start] = round(sp_total, 4)

        periods = []
        for result in resp["ResultsByTime"]:
            period_start = result["TimePeriod"]["Start"]
            services = []
            for group in result["Groups"]:
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                if amount < 0.001:
                    continue
                services.append({
                    "service": group["Keys"][0],
                    "cost": round(amount, 4),
                    "unit": group["Metrics"]["UnblendedCost"]["Unit"],
                })
            services.sort(key=lambda x: x["cost"], reverse=True)
            usage_total = sum(s["cost"] for s in services)
            sp_fee = sp_fee_by_period.get(period_start, 0)
            periods.append({
                "start": period_start,
                "end": result["TimePeriod"]["End"],
                "total": round(usage_total, 4),
                "sp_fee": sp_fee,
                "services": services,
            })

        return {
            "account_id": account_id,
            "periods": periods,
            "currency": "USD",
        }
    except Exception as e:
        return {"account_id": account_id, "error": str(e), "periods": []}


def get_all_accounts_cost_summary(
    account_ids: list,
    start: str = None,
    end: str = None,
) -> list:
    if not end:
        end = date.today().isoformat()
    if not start:
        start = date.today().replace(day=1).isoformat()

    session = get_management_session()
    ce = session.client("ce", region_name="us-east-1")

    try:
        # 실사용 비용 (SP/RI 수수료·세금 제외)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": SP_RI_EXCLUDE_TYPES}}},
            GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
            Metrics=["UnblendedCost"],
        )

        cost_map = {}
        for result in resp["ResultsByTime"]:
            for group in result["Groups"]:
                acc_id = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                # 음수(EDP 단독 잔존 등)는 0으로 처리
                cost_map[acc_id] = cost_map.get(acc_id, 0) + max(amount, 0)

        # SP 수수료 별도 조회 (표시용)
        sp_resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": SP_FEE_TYPES}},
            GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
            Metrics=["UnblendedCost"],
        )

        sp_map = {}
        for result in sp_resp["ResultsByTime"]:
            for group in result["Groups"]:
                acc_id = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                sp_map[acc_id] = sp_map.get(acc_id, 0) + amount

        return [
            {
                "account_id": acc_id,
                "cost": round(cost_map.get(acc_id, 0), 4),
                "sp_fee": round(sp_map.get(acc_id, 0), 4),
                "currency": "USD",
            }
            for acc_id in account_ids
        ]
    except Exception as e:
        return [{"account_id": acc_id, "cost": 0, "sp_fee": 0, "error": str(e)} for acc_id in account_ids]
