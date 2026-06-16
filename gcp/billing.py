from __future__ import annotations
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Tuple

SETTINGS_FILE = Path.home() / ".gcp_audit_billing_settings.json"


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {"bq_project": "", "bq_dataset": ""}


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False))


def fetch_costs(bq_project: str, bq_dataset: str, on_progress=None) -> dict:
    """
    BigQuery 빌링 익스포트를 쿼리하여 프로젝트별 서비스별 비용 반환.
    Returns: {project_id: {"_total": float, "_currency": str,
              "_month": str, "_monthly": {month: total}, service: float, ...}}
    """
    try:
        from google.cloud import bigquery
    except ImportError:
        raise RuntimeError(
            "google-cloud-bigquery 패키지가 필요합니다.\n"
            "pip install google-cloud-bigquery"
        )

    if on_progress:
        on_progress(5, "BigQuery 클라이언트 초기화 중...")
    client = bigquery.Client()

    if on_progress:
        on_progress(10, "빌링 익스포트 테이블 탐색 중...")
    try:
        tables = list(client.list_tables(f"{bq_project}.{bq_dataset}"))
    except Exception as e:
        raise RuntimeError(
            f"데이터셋 접근 실패: {bq_project}.{bq_dataset}\n{e}"
        )

    billing_tables = [
        t.table_id for t in tables
        if "gcp_billing_export" in t.table_id.lower()
    ]
    if not billing_tables:
        raise RuntimeError(
            f"gcp_billing_export 테이블 없음: {bq_project}.{bq_dataset}\n"
            "Cloud Billing → 빌링 익스포트 → BigQuery로 내보내기를 설정하세요."
        )

    table_path = f"`{bq_project}.{bq_dataset}.{billing_tables[0]}`"
    if on_progress:
        on_progress(20, f"쿼리 중: {billing_tables[0]}")

    query = f"""
    SELECT
      COALESCE(project.id, '_unknown') AS project_id,
      service.description               AS service,
      ROUND(SUM(cost), 2)               AS total_cost,
      MIN(currency)                     AS currency,
      FORMAT_DATE('%Y-%m', DATE(usage_start_time)) AS month
    FROM {table_path}
    WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
      AND cost > 0
    GROUP BY project_id, service, month
    ORDER BY project_id, month DESC, total_cost DESC
    """

    if on_progress:
        on_progress(30, "쿼리 실행 중... (수십 초 소요될 수 있습니다)")
    try:
        rows = list(client.query(query).result())
    except Exception as e:
        raise RuntimeError(f"BigQuery 쿼리 실패: {e}")

    if on_progress:
        on_progress(85, f"{len(rows):,}행 데이터 처리 중...")

    # pid -> month -> {svc: cost}
    monthly: dict = {}
    for row in rows:
        pid = row.project_id
        svc = row.service or "기타"
        cost = float(row.total_cost or 0)
        month = row.month or ""
        monthly.setdefault(pid, {}).setdefault(month, {})
        monthly[pid][month][svc] = monthly[pid][month].get(svc, 0) + cost

    cost_map: dict = {}
    for pid, months in monthly.items():
        sorted_m = sorted(months.keys(), reverse=True)
        latest = sorted_m[0] if sorted_m else ""
        svcs = months.get(latest, {})
        total = sum(svcs.values())
        cost_map[pid] = {
            "_total":    round(total, 2),
            "_currency": "USD",
            "_month":    latest,
            "_monthly":  {m: round(sum(s.values()), 2) for m, s in months.items()},
            **{k: round(v, 2) for k, v in svcs.items()},
        }

    if on_progress:
        on_progress(100, "빌링 스캔 완료!")
    return cost_map


# ── 인보이스 모드 (BigQuery 없이) ────────────────────────────────────

def _get_access_token() -> str:
    from gcp.gcp import _gcloud
    stdout, _, _ = _gcloud("auth", "print-access-token")
    return stdout.strip()


def _parse_money(amt: dict) -> float:
    """Google Money 타입(units + nanos)을 float으로 변환."""
    units = int(amt.get("units") or 0)
    nanos = int(amt.get("nanos") or 0)
    return round(units + nanos / 1e9, 2)


def fetch_account_invoices(billing_account_ids: list[str], on_progress=None) -> dict:
    """
    Cloud Billing 인보이스 API로 빌링 계정별 월별 청구 합계를 조회.
    BigQuery 익스포트 없이 사용 가능하나 프로젝트별 세분화는 불가.

    Returns: {
        billing_account_id: {
            "_total":    float,   # 최근 월 합계
            "_currency": str,
            "_month":    str,     # 최근 인보이스 월 (YYYY-MM)
            "_monthly":  {month: amount},
            "_mode":     "invoice",
        }
    }
    """
    if on_progress:
        on_progress(5, "Cloud Billing 인보이스 API 조회 중...")

    try:
        token = _get_access_token()
    except Exception as e:
        raise RuntimeError(f"gcloud 인증 토큰 취득 실패: {e}")

    result: dict = {}
    total = len(billing_account_ids)

    for idx, bid in enumerate(billing_account_ids):
        if on_progress:
            on_progress(10 + int(idx / total * 85),
                        f"({idx+1}/{total}) 인보이스 조회: {bid}")
        try:
            url = (f"https://cloudbilling.googleapis.com/v1/"
                   f"billingAccounts/{bid}/invoices")
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())

            invoices = data.get("invoices", [])
            monthly: dict[str, float] = {}
            currency = "USD"

            for inv in invoices[:6]:      # 최근 6개월
                date = inv.get("invoiceDate", {})
                year = date.get("year")
                month = date.get("month")
                if not year or not month:
                    continue
                month_key = f"{year}-{str(month).zfill(2)}"
                # subtotalAmount = 세금 전 합계
                amt = inv.get("subtotalAmount") or inv.get("totalAmount") or {}
                currency = amt.get("currencyCode", "USD")
                monthly[month_key] = _parse_money(amt)

            latest = max(monthly.keys()) if monthly else ""
            result[bid] = {
                "_total":    monthly.get(latest, 0),
                "_currency": currency,
                "_month":    latest,
                "_monthly":  monthly,
                "_mode":     "invoice",
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 인보이스 API는 카드 자동결제 계정에서는 지원되지 않음
                error_msg = "인보이스 API 미지원 (카드 자동결제 계정). BigQuery 빌링 익스포트를 설정하세요."
            elif e.code == 403:
                error_msg = "권한 없음 (roles/billing.viewer 이상 필요)"
            else:
                error_msg = f"HTTP {e.code}: {e.reason}"
            result[bid] = {
                "_total": 0, "_currency": "USD",
                "_month": "", "_monthly": {},
                "_mode": "invoice",
                "_error": error_msg,
            }
        except Exception as e:
            result[bid] = {
                "_total": 0, "_currency": "USD",
                "_month": "", "_monthly": {},
                "_mode": "invoice",
                "_error": str(e),
            }

    if on_progress:
        on_progress(100, "인보이스 조회 완료!")
    return result


# ── Recommender API 프로젝트별 비용 조회 ─────────────────────────────

def fetch_project_costs_recommender(projects: list[dict], on_progress=None, quota_project: str = "") -> dict:
    """
    Recommender API (google.billing.CostInsight)로 프로젝트별 비용 조회.
    BigQuery 익스포트 없이 프로젝트 단위 실제 비용 확인 가능.
    quota_project: x-goog-user-project 헤더에 사용할 GCP 프로젝트 ID (미설정 시 settings에서 자동 로드)

    Returns: {project_id: {"_total": float, "_currency": str, "_month": str}}
    """
    import google.auth.transport.requests
    from requests.adapters import HTTPAdapter
    from gcp.gcp import _get_credentials

    pids = [p["project_id"] for p in projects if p.get("project_id")]
    if not pids:
        return {}

    # quota project 결정: 인자 > 설정 파일 bq_project > 첫 번째 프로젝트 ID
    if not quota_project:
        quota_project = load_settings().get("bq_project", "")
    if not quota_project and pids:
        quota_project = pids[0]

    if on_progress:
        on_progress(5, "Recommender API 인증 중...")

    try:
        creds = _get_credentials()
        session = google.auth.transport.requests.AuthorizedSession(creds)
        session.mount("https://", HTTPAdapter(pool_connections=5, pool_maxsize=30))
        # quota project 헤더 설정 (Application Default Credentials 사용 시 필수)
        if quota_project:
            session.headers.update({"x-goog-user-project": quota_project})
    except Exception as e:
        raise RuntimeError(f"인증 실패: {e}")

    BASE = "https://recommender.googleapis.com/v1"

    def _fetch_one(pid: str) -> Tuple[str, Optional[dict]]:
        try:
            url = (f"{BASE}/projects/{pid}/locations/global"
                   f"/insightTypes/google.billing.CostInsight/insights")
            r = session.get(url, timeout=15)
            if r.status_code in (400, 403, 404):
                return pid, None
            r.raise_for_status()
            data = r.json()
            insights = data.get("insights", [])
            if not insights:
                return pid, None

            for insight in insights:
                state = insight.get("stateInfo", {}).get("state", "")
                if state == "DISMISSED":
                    continue
                content = insight.get("content", {})
                overview = content.get("overview", content)

                amount: Optional[float] = None
                currency = "USD"
                month_str = ""

                # 여러 필드명 패턴 순서대로 시도
                for field in ("monthlySpend", "lastMonthCost", "costLast30Days",
                               "currentMonthCost", "cost", "amount"):
                    val = overview.get(field)
                    if val is None:
                        continue
                    if isinstance(val, dict):
                        # Google Money 타입 {units, nanos, currencyCode}
                        amount = _parse_money(val)
                        currency = val.get("currencyCode", currency)
                    else:
                        try:
                            amount = float(val)
                        except (TypeError, ValueError):
                            continue
                    break

                if amount is None:
                    continue

                # 통화 및 월 정보
                currency = overview.get("currencyCode", currency)
                month_str = (overview.get("period") or
                             overview.get("month") or
                             insight.get("lastRefreshTime", "")[:7])

                return pid, {
                    "_total":    round(amount, 2),
                    "_currency": currency,
                    "_month":    month_str,
                }

            return pid, None
        except Exception:
            return pid, None

    total = len(pids)
    result: dict = {}
    done = 0

    with ThreadPoolExecutor(max_workers=min(total, 30)) as ex:
        futures = {ex.submit(_fetch_one, pid): pid for pid in pids}
        for fut in as_completed(futures):
            pid, cost = fut.result()
            if cost is not None:
                result[pid] = cost
            done += 1
            if on_progress and done % 30 == 0:
                pct = 10 + int(done / total * 85)
                on_progress(pct, f"Recommender API 조회 중... ({done}/{total})")

    if on_progress:
        on_progress(100, f"프로젝트별 비용 조회 완료 ({len(result)}/{total}개 데이터 수집)")
    return result


# ── 빌링 계정 현황 스캔 (BigQuery 없이) ──────────────────────────────

def fetch_billing_accounts(projects: list[dict], on_progress=None) -> dict:
    """
    Cloud Billing SDK로 빌링 계정 현황 조회 (gcloud subprocess 없음).
    Returns: {
        billing_account_id: {
            "display_name": str,
            "open": bool,
            "currency": str,
            "master_billing_account": str,
            "project_count": int,
            "project_ids": [str],
            "_mode": "account_overview",
        }
    }
    """
    import google.auth.transport.requests
    from requests.adapters import HTTPAdapter
    from gcp.gcp import _get_credentials

    if on_progress:
        on_progress(5, "빌링 계정 목록 조회 중...")

    # REST API 사용: gRPC billing_v1은 EC2 gcloud 토큰 환경에서 ACCESS_TOKEN_TYPE_UNSUPPORTED
    try:
        creds = _get_credentials()
        session = google.auth.transport.requests.AuthorizedSession(creds)
        session.mount("https://", HTTPAdapter(pool_connections=5, pool_maxsize=20))

        accounts_raw = []
        url: str | None = "https://cloudbilling.googleapis.com/v1/billingAccounts"
        while url:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            accounts_raw.extend(data.get("billingAccounts", []))
            pt = data.get("nextPageToken")
            url = f"https://cloudbilling.googleapis.com/v1/billingAccounts?pageToken={pt}" if pt else None
    except Exception as e:
        raise RuntimeError(f"빌링 계정 목록 조회 실패: {e}")

    if on_progress:
        on_progress(20, f"빌링 계정 {len(accounts_raw)}개 확인. 프로젝트 연결 정보 조합 중...")

    # 프로젝트 목록에서 billing_account_id → project 매핑
    pid_to_bid: dict[str, str] = {
        p["project_id"]: p["billing_account_id"]
        for p in projects
        if p.get("billing_account_id")
    }
    bid_to_pids: dict[str, list[str]] = {}
    for pid, bid in pid_to_bid.items():
        bid_to_pids.setdefault(bid, []).append(pid)

    if on_progress:
        on_progress(70, "데이터 조합 중...")

    result: dict = {}
    for acc in accounts_raw:
        bid = acc.get("name", "").replace("billingAccounts/", "")
        if not bid:
            continue
        master_id = acc.get("masterBillingAccount", "").replace("billingAccounts/", "")
        pids = bid_to_pids.get(bid, [])
        result[bid] = {
            "display_name":           acc.get("displayName", ""),
            "open":                   acc.get("open", False),
            "currency":               acc.get("currencyCode", ""),
            "master_billing_account": master_id,
            "project_count":          len(pids),
            "project_ids":            pids,
            "_mode":                  "account_overview",
        }

    # 접근 불가 빌링 계정: list_billing_accounts에 없지만 프로젝트에 연결된 계정
    known_bids = set(result.keys())
    for bid, pids in bid_to_pids.items():
        if bid not in known_bids:
            result[bid] = {
                "display_name":           f"(접근 불가) {bid}",
                "open":                   None,
                "currency":               "",
                "master_billing_account": "",
                "project_count":          len(pids),
                "project_ids":            pids,
                "_mode":                  "account_overview",
            }

    # ── 프로젝트 목록이 없으면 빌링 계정 API에서 직접 수집 ───────────────
    if on_progress:
        on_progress(72, "빌링 계정별 프로젝트 목록 수집 중...")

    def _fetch_bid_projects(bid: str):
        """billingAccounts/{bid}/projects API로 프로젝트 목록 조회."""
        pids = []
        try:
            url = f"https://cloudbilling.googleapis.com/v1/billingAccounts/{bid}/projects"
            r = session.get(url, timeout=10)
            if r.ok:
                for p in r.json().get("projectBillingInfo", []):
                    pid = p.get("projectId")
                    if pid and p.get("billingEnabled"):
                        pids.append(pid)
        except Exception:
            pass
        return bid, pids

    # 모든 빌링 계정에 대해 프로젝트 목록 수집 (없으면 API로 조회)
    bids_to_fetch = list(result.keys())
    if bids_to_fetch:
        with ThreadPoolExecutor(max_workers=min(len(bids_to_fetch), 10)) as ex:
            for bid, pids in ex.map(_fetch_bid_projects, bids_to_fetch):
                if pids:
                    result[bid]["project_ids"] = pids
                    result[bid]["project_count"] = len(pids)

    # ── 비용 조회: 청구서 API 병렬 호출 ─────────────────────────────────
    if on_progress:
        on_progress(82, "빌링 계정 비용 조회 중 (인보이스 API)...")

    try:
        token = _get_access_token()
    except Exception:
        token = ""

    def _fetch_invoice(bid: str) -> tuple[str, dict]:
        """단일 빌링 계정의 최신 인보이스 비용 반환."""
        empty = {"_total": None, "_currency": "", "_month": "", "_monthly": {}}
        if not token:
            return bid, empty
        try:
            url = f"https://cloudbilling.googleapis.com/v1/billingAccounts/{bid}/invoices"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            invoices = data.get("invoices", [])
            monthly: dict[str, float] = {}
            currency = ""
            for inv in invoices[:6]:
                date = inv.get("invoiceDate", {})
                year, month = date.get("year"), date.get("month")
                if not year or not month:
                    continue
                key = f"{year}-{str(month).zfill(2)}"
                amt = inv.get("subtotalAmount") or inv.get("totalAmount") or {}
                currency = amt.get("currencyCode", "") or currency
                monthly[key] = _parse_money(amt)
            latest = max(monthly.keys()) if monthly else ""
            return bid, {
                "_total":    monthly.get(latest, 0) if monthly else None,
                "_currency": currency,
                "_month":    latest,
                "_monthly":  monthly,
            }
        except Exception:
            return bid, empty

    bid_list = list(result.keys())
    with ThreadPoolExecutor(max_workers=min(len(bid_list), 20)) as ex:
        for bid, cost in ex.map(_fetch_invoice, bid_list):
            if bid in result:
                result[bid].update(cost)

    # ── Recommender API로 프로젝트별 실제 비용 합산 ──────────────────────
    if on_progress:
        on_progress(92, "Recommender API로 프로젝트별 비용 추정 중...")

    # 빌링 계정 전체의 프로젝트 ID 수집
    all_pids = []
    for info in result.values():
        all_pids.extend(info.get("project_ids", []))
    all_pids = list(set(all_pids))

    if all_pids:
        try:
            # project_id → billing_account_id 역매핑
            pid_to_bid_map = {}
            for bid, info in result.items():
                for pid in info.get("project_ids", []):
                    pid_to_bid_map[pid] = bid

            # fetch_project_costs_recommender 에 맞는 포맷으로 변환
            # quota project: 빌링 계정 보유 프로젝트 중 첫 번째 사용
            quota_proj = all_pids[0] if all_pids else ""
            proj_list = [{"project_id": pid} for pid in all_pids]
            proj_costs = fetch_project_costs_recommender(proj_list, quota_project=quota_proj)

            for pid, pcost in proj_costs.items():
                bid = pid_to_bid_map.get(pid, "")
                if bid and bid in result:
                    result[bid].setdefault("_projects", {})[pid] = pcost

            # 빌링 계정 합계를 Recommender 합계로 대체 (인보이스보다 정확한 경우)
            for bid, info in result.items():
                proj_data = info.get("_projects", {})
                if proj_data:
                    rec_total = sum(p.get("_total", 0) for p in proj_data.values())
                    currencies = [p.get("_currency", "KRW") for p in proj_data.values() if p.get("_currency")]
                    # 인보이스 비용이 없는 경우 Recommender 합계로 채움
                    if info.get("_total") is None and rec_total > 0:
                        result[bid]["_total"] = round(rec_total, 2)
                        result[bid]["_currency"] = currencies[0] if currencies else "KRW"
                    result[bid]["_rec_total"] = round(rec_total, 2)
                    result[bid]["_rec_currency"] = currencies[0] if currencies else "KRW"

        except Exception as rec_err:
            if on_progress:
                on_progress(96, f"Recommender API 조회 실패 (무시): {rec_err}")

    if on_progress:
        on_progress(100, f"빌링 계정 {len(result)}개 조회 완료!")
    return result
