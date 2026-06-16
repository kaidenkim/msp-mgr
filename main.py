from __future__ import annotations
import asyncio
import json
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# GCP imports
from gcp.gcp import get_auth_info, full_scan, scan_billing_resources, delete_project, EXTRA_GROUPS, scan_extra_resources, get_resource_details
from gcp.export import generate_excel
from gcp.billing import (load_settings, save_settings, fetch_costs,
                          fetch_account_invoices, fetch_billing_accounts,
                          fetch_project_costs_recommender)

# AWS imports
from aws.services.organizations import list_accounts
from aws.services.organizations_tree import get_ou_tree, get_all_ous_flat, get_account_parent, move_account
from aws.services.cost_explorer import get_account_costs, get_all_accounts_cost_summary
from aws.services.cmdb import fetch_cmdb_data, get_cmdb_summary, get_cmdb_account, trigger_collection, get_collection_status, _cache as cmdb_cache
from aws.services.resource_collector import collect_account_resources

from auth import is_admin, require_admin, verify_admin_password, change_admin_password, make_session, COOKIE_NAME, SESSION_MAX_AGE
import config as _config

# ── 캐시 파일 ─────────────────────────────────────────────────────────
_CACHE_DIR               = Path(os.environ.get("MSP_CACHE_DIR", str(Path.home())))
CACHE_FILE               = _CACHE_DIR / ".msp_gcp_audit_cache.json"
RESOURCE_CACHE_FILE      = _CACHE_DIR / ".msp_gcp_resource_cache.json"
BILLING_COST_FILE        = _CACHE_DIR / ".msp_gcp_billing_costs.json"
CMDB_DETAIL_CACHE_FILE   = _CACHE_DIR / ".msp_gcp_cmdb_detail_cache.json"
HISTORY_DIR              = _CACHE_DIR / ".msp_history"
HISTORY_KEEP_DAYS        = 90

def _save_history(kind: str, payload: dict) -> None:
    """날짜별 히스토리 파일 저장 (kind: gcp_projects | gcp_resources | aws_cmdb)."""
    try:
        HISTORY_DIR.mkdir(exist_ok=True)
        today = date.today().isoformat()
        path = HISTORY_DIR / f"{kind}_{today}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False))
        # 오래된 파일 정리
        cutoff = date.today().toordinal() - HISTORY_KEEP_DAYS
        for f in HISTORY_DIR.glob(f"{kind}_*.json"):
            try:
                d = date.fromisoformat(f.stem.replace(f"{kind}_", ""))
                if d.toordinal() < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass

def _list_history_dates(kind: str) -> List[str]:
    """저장된 날짜 목록 반환 (최신순)."""
    if not HISTORY_DIR.exists():
        return []
    dates = []
    for f in HISTORY_DIR.glob(f"{kind}_*.json"):
        try:
            d = f.stem.replace(f"{kind}_", "")
            date.fromisoformat(d)  # 유효성 검사
            dates.append(d)
        except Exception:
            pass
    return sorted(dates, reverse=True)

def _load_history(kind: str, target_date: str) -> Optional[dict]:
    """특정 날짜 히스토리 로드."""
    path = HISTORY_DIR / f"{kind}_{target_date}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return None

# 메모리 캐시: {type: {project_id: [items]}}
_cmdb_detail_cache: dict = {}
_cmdb_detail_built_at: Optional[str] = None

def _load_cmdb_detail_cache():
    """서버 시작 시 캐시 파일 로드."""
    global _cmdb_detail_cache, _cmdb_detail_built_at
    if CMDB_DETAIL_CACHE_FILE.exists():
        try:
            data = json.loads(CMDB_DETAIL_CACHE_FILE.read_text())
            _cmdb_detail_cache = data.get("cache", {})
            _cmdb_detail_built_at = data.get("built_at")
        except Exception:
            pass

_load_cmdb_detail_cache()

# ── 로그인 HTML ───────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>MSP Manager — 로그인</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; display: flex; align-items: center;
       justify-content: center; height: 100vh; }
.box { background: #1a1d27; border: 1px solid #252840; border-radius: 12px;
       padding: 36px 32px; width: 340px; }
h2 { font-size: 18px; font-weight: 700; color: #fff; margin-bottom: 24px; }
h2 span { color: #6366f1; }
label { font-size: 11px; color: #64748b; margin-bottom: 4px; display: block; }
input { width: 100%; background: #0f1117; border: 1px solid #252840;
        color: #e2e8f0; padding: 9px 12px; border-radius: 7px; font-size: 13px;
        margin-bottom: 16px; }
button { width: 100%; background: #6366f1; color: #fff; border: none;
         padding: 10px; border-radius: 7px; font-size: 13px; font-weight: 600;
         cursor: pointer; }
.err { color: #f87171; font-size: 12px; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="box">
  <h2>MSP <span>Manager</span></h2>
  <!--ERROR-->
  <form method="post" action="/login">
    <label>사용자 이름</label>
    <input name="username" value="admin" readonly>
    <label>비밀번호</label>
    <input name="password" type="password" autofocus>
    <button type="submit">로그인</button>
  </form>
</div>
</body>
</html>"""

# ── auth 캐시 ─────────────────────────────────────────────────────────
_auth_cache: Optional[dict] = None
_auth_cache_lock = threading.Lock()


def _refresh_auth_cache():
    global _auth_cache
    while True:
        try:
            info = get_auth_info()
            with _auth_cache_lock:
                _auth_cache = info
        except Exception:
            pass
        time.sleep(60)


def _warmup_credentials():
    try:
        from gcp.gcp import _get_credentials
        from google.cloud import resourcemanager_v3
        creds = _get_credentials()
        resourcemanager_v3.ProjectsClient(credentials=creds)
    except Exception:
        pass


# ── 전역 상태 ─────────────────────────────────────────────────────────
_state: dict = {
    "status": "idle", "pct": 0, "stage": "", "projects": [], "last_scan": None, "error": None,
}
_progress_q: queue.Queue = queue.Queue()
_scan_running = False

_res_state: dict = {
    "status": "idle", "pct": 0, "stage": "", "projects": [], "last_scan": None, "error": None,
}
_res_q: queue.Queue = queue.Queue()
_res_running = False

_billing_state: dict = {
    "status": "idle", "pct": 0, "stage": "", "costs": {}, "mode": "", "last_scan": None, "error": None,
}
_billing_q: queue.Queue = queue.Queue()
_billing_running = False

_extra_states: dict = {
    g: {"status": "idle", "pct": 0, "stage": "", "last_scan": None, "error": None}
    for g in EXTRA_GROUPS
}
_extra_q: queue.Queue = queue.Queue()
_extra_running: Optional[str] = None


async def _load_cache():
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            _state["projects"] = data.get("projects", [])
            _state["last_scan"] = data.get("last_scan")
            if _state["projects"]:
                _state["status"] = "done"
        except Exception:
            pass
    if RESOURCE_CACHE_FILE.exists():
        try:
            data = json.loads(RESOURCE_CACHE_FILE.read_text())
            _res_state["projects"] = data.get("projects", [])
            _res_state["last_scan"] = data.get("last_scan")
            if _res_state["projects"]:
                _res_state["status"] = "done"
        except Exception:
            pass
    if BILLING_COST_FILE.exists():
        try:
            data = json.loads(BILLING_COST_FILE.read_text())
            _billing_state["costs"] = data.get("costs", {})
            _billing_state["mode"] = data.get("mode", "")
            _billing_state["last_scan"] = data.get("last_scan")
            if _billing_state["costs"]:
                _billing_state["status"] = "done"
        except Exception:
            pass


# ── 예약 이동 스케줄러 ─────────────────────────────────────────────────
SCHEDULED_MOVES_FILE = Path(__file__).parent / "data" / "scheduled_moves.json"

def _load_scheduled_moves() -> List[dict]:
    try:
        if SCHEDULED_MOVES_FILE.exists():
            return json.loads(SCHEDULED_MOVES_FILE.read_text())
    except Exception:
        pass
    return []

def _save_scheduled_moves(moves: List[dict]):
    SCHEDULED_MOVES_FILE.parent.mkdir(exist_ok=True)
    SCHEDULED_MOVES_FILE.write_text(json.dumps(moves, ensure_ascii=False, indent=2))

def _safe_move_account(account_id: str, return_ou_id: str):
    """현재 위치 확인 후 안전하게 이동. DuplicateAccountException 등 무시."""
    try:
        parent = get_account_parent(account_id)
        current_parent_id = parent.get("id", "")
        # 이미 복귀 OU에 있으면 스킵
        if not current_parent_id or current_parent_id == return_ou_id:
            return
        move_account(account_id, current_parent_id, return_ou_id)
        _aws_bust("org_tree")
        _aws_bust("org_ous")
    except Exception as e:
        err = str(e)
        # 이미 해당 위치에 있거나 중복 이동 → 정상 처리
        if "DuplicateAccountException" in err or "already present" in err:
            _aws_bust("org_tree")
            _aws_bust("org_ous")
        # 그 외 오류는 무시 (다음 주기에 재시도)


def _run_scheduled_moves():
    """1시간마다 예약 이동 만료 여부 확인 후 자동 복귀 (날짜 단위라 충분)"""
    while True:
        try:
            today = date.today().isoformat()
            moves = _load_scheduled_moves()
            remaining = []
            for m in moves:
                if m.get("return_date", "9999-99-99") <= today:
                    _safe_move_account(m["account_id"], m["return_ou_id"])
                    # 만료된 항목은 결과와 무관하게 제거
                else:
                    remaining.append(m)
            if len(remaining) != len(moves):
                _save_scheduled_moves(remaining)
        except Exception:
            pass
        time.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _load_cache()
    threading.Thread(target=_refresh_auth_cache, daemon=True).start()
    threading.Thread(target=_warmup_credentials, daemon=True).start()
    threading.Thread(target=_run_scheduled_moves, daemon=True).start()
    yield


app = FastAPI(title="MSP Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── SPA / 인증 ────────────────────────────────────────────────────────
@app.get("/")
async def root(request: Request):
    role = "admin" if is_admin(request) else "viewer"
    html = (Path("static") / "index.html").read_text()
    html = html.replace("{{ role }}", role)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_admin(request):
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(LOGIN_HTML)


@app.post("/login")
async def do_login(request: Request, username: str = Form(""), password: str = Form(...)):
    if username != "admin" or not verify_admin_password(password):
        return HTMLResponse(
            LOGIN_HTML.replace("<!--ERROR-->", "<p class='err'>비밀번호 오류</p>"),
            status_code=401
        )
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE_NAME, make_session(), max_age=SESSION_MAX_AGE, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Admin ─────────────────────────────────────────────────────────────
@app.post("/api/admin/change-password")
async def change_password(request: Request, _: dict = Depends(require_admin)):
    body = await request.json()
    current = body.get("current_password", "")
    new_pw = body.get("new_password", "")
    if not verify_admin_password(current):
        raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="비밀번호는 8자 이상이어야 합니다")
    change_admin_password(new_pw)
    return {"ok": True}


# ── 통합 대시보드 ─────────────────────────────────────────────────────
@app.get("/api/overview")
async def api_overview():
    projects = _state.get("projects", [])
    billing_connected = sum(1 for p in projects if p.get("billing_account_id"))
    total_resources = sum(
        p.get("total_resources", 0) for p in _res_state.get("projects", [])
    )
    try:
        aws_accounts = list_accounts()
        aws_total = len(aws_accounts)
    except Exception:
        aws_total = 0
    return {
        "gcp": {
            "total_projects": len(projects),
            "billing_connected": billing_connected,
            "total_resources": total_resources,
            "last_scan": _state.get("last_scan"),
        },
        "aws": {
            "total_accounts": aws_total,
            "last_scan": None,
        },
    }


# ── GCP API ───────────────────────────────────────────────────────────
@app.get("/api/gcp/status")
async def api_gcp_status():
    with _auth_cache_lock:
        auth = _auth_cache
    return {
        "account":   auth.get("account") if auth else None,
        "status":    _state["status"],
        "pct":       _state["pct"],
        "stage":     _state["stage"],
        "last_scan": _state["last_scan"],
        "error":     _state["error"],
        "projects":  _state["projects"],
    }


def _run_scan():
    global _scan_running
    _scan_running = True
    _state["status"] = "scanning"
    _state["error"] = None

    def on_progress(pct, stage, done=0, total=0):
        _state["pct"] = pct
        _state["stage"] = stage
        _progress_q.put({"type": "progress", "pct": pct, "stage": stage, "done": done, "total": total})

    try:
        projects = full_scan(on_progress)
        _state["projects"] = projects
        _state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _state["status"] = "done"
        payload = {"projects": projects, "last_scan": _state["last_scan"]}
        CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False))
        _save_history("gcp_projects", payload)
        _progress_q.put({"type": "done"})
    except Exception as exc:
        _state["status"] = "error"
        _state["error"] = str(exc)
        _progress_q.put({"type": "error", "message": str(exc)})
    finally:
        _scan_running = False


@app.post("/api/gcp/scan")
async def api_gcp_scan_start():
    global _scan_running
    if _scan_running:
        return {"status": "already_running"}
    while not _progress_q.empty():
        try:
            _progress_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/gcp/scan/stream")
async def api_gcp_scan_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _progress_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _scan_running:
                    break
            await asyncio.sleep(0.4)
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/gcp/export")
async def api_gcp_export():
    if not _state["projects"]:
        return Response(content="데이터 없음. 먼저 스캔을 실행하세요.", status_code=400)
    xlsx_bytes = generate_excel(_state["projects"])
    filename = f"gcp_projects_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/gcp/projects/delete")
async def api_gcp_delete_projects(request: Request, _: dict = Depends(require_admin)):
    body = await request.json()
    project_ids = body.get("project_ids", [])
    if not project_ids:
        return {"status": "error", "message": "삭제할 프로젝트 ID가 없습니다."}
    results = []
    for pid in project_ids:
        success, msg = delete_project(pid)
        results.append({"project_id": pid, "success": success, "message": msg})
        if success:
            _state["projects"] = [p for p in _state["projects"] if p["project_id"] != pid]
    CACHE_FILE.write_text(json.dumps({"projects": _state["projects"], "last_scan": _state["last_scan"]}, ensure_ascii=False))
    return {"status": "done", "results": results}


def _run_resource_scan():
    global _res_running
    _res_running = True
    _res_state["status"] = "scanning"
    _res_state["error"] = None

    def on_progress(pct, stage, done=0, total=0):
        _res_state["pct"] = pct
        _res_state["stage"] = stage
        _res_q.put({"type": "progress", "pct": pct, "stage": stage, "done": done, "total": total})

    try:
        billing = [p for p in _state["projects"] if p.get("deletable") == "빌링연결"]
        if not billing:
            raise RuntimeError("빌링 연결된 프로젝트가 없습니다. 전체 스캔을 먼저 실행하세요.")
        result = scan_billing_resources(billing, on_progress)
        _res_state["projects"] = result
        _res_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _res_state["status"] = "done"
        res_payload = {"projects": result, "last_scan": _res_state["last_scan"]}
        RESOURCE_CACHE_FILE.write_text(json.dumps(res_payload, ensure_ascii=False))
        _save_history("gcp_resources", res_payload)
        _res_q.put({"type": "done"})
    except Exception as exc:
        _res_state["status"] = "error"
        _res_state["error"] = str(exc)
        _res_q.put({"type": "error", "message": str(exc)})
    finally:
        _res_running = False


@app.post("/api/gcp/resources/scan")
async def api_gcp_resource_scan_start():
    global _res_running
    if _res_running:
        return {"status": "already_running"}
    if not _state["projects"]:
        return {"status": "error", "message": "전체 프로젝트 스캔을 먼저 실행하세요."}
    while not _res_q.empty():
        try:
            _res_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_resource_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/gcp/resources/stream")
async def api_gcp_resource_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _res_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _res_running:
                    break
            await asyncio.sleep(0.4)
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/gcp/resources")
async def api_gcp_resources():
    return {
        "status":    _res_state["status"],
        "pct":       _res_state["pct"],
        "stage":     _res_state["stage"],
        "last_scan": _res_state["last_scan"],
        "error":     _res_state["error"],
        "projects":  _res_state["projects"],
    }


@app.get("/api/gcp/resources/detail")
async def api_gcp_resource_detail(project_id: str, type: str):
    """프로젝트 + 리소스 타입의 상세 목록 반환. 캐시 우선, 없으면 on-demand."""
    # 캐시 히트
    if type in _cmdb_detail_cache and project_id in _cmdb_detail_cache[type]:
        return {"project_id": project_id, "type": type, "items": _cmdb_detail_cache[type][project_id], "cached": True}
    try:
        items = await asyncio.get_event_loop().run_in_executor(
            None, get_resource_details, project_id, type
        )
        return {"project_id": project_id, "type": type, "items": items, "cached": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gcp/cmdb/cache/data")
async def api_gcp_cmdb_cache_data(type: str):
    """캐시된 타입 전체 flat 목록 반환 (프론트 새로고침 최적화용)."""
    type_cache = _cmdb_detail_cache.get(type, {})
    all_items = []
    for project_id, items in type_cache.items():
        for item in items:
            all_items.append({**item, "_project": project_id})
    return {"type": type, "items": all_items, "built_at": _cmdb_detail_built_at, "cached": True}


@app.get("/api/gcp/cmdb/cache/status")
async def api_gcp_cmdb_cache_status():
    """CMDB 상세 캐시 빌드 상태 반환."""
    return {
        "built_at": _cmdb_detail_built_at,
        "types": list(_cmdb_detail_cache.keys()),
        "total_projects": sum(len(v) for v in _cmdb_detail_cache.values()),
    }


# 캐시 빌드 상태
_cmdb_build_running = False
_cmdb_build_state: dict = {"status": "idle", "pct": 0, "stage": "", "error": None}
_cmdb_build_q: queue.Queue = queue.Queue()

def _run_cmdb_detail_build():
    """모든 타입 × 모든 프로젝트 상세를 미리 수집해 캐시 파일로 저장."""
    global _cmdb_build_running, _cmdb_detail_cache, _cmdb_detail_built_at
    _cmdb_build_running = True
    _cmdb_build_state["status"] = "building"
    _cmdb_build_state["error"] = None

    ALL_TYPES = ["vm", "run", "fn", "gke", "storage", "sql", "pubsub", "vpc", "lb", "sa", "sink"]
    RES_KEY_MAP = {"vm":"vm","run":"run","fn":"functions","gke":"gke","storage":"storage",
                   "sql":"sql","pubsub":"pubsub","vpc":"vpc","lb":"lb","sa":"sa","sink":"log_sink"}

    projects = _res_state.get("projects", [])
    if not projects:
        _cmdb_build_state["status"] = "error"
        _cmdb_build_state["error"] = "리소스 스캔 데이터 없음. 먼저 리소스 스캔을 실행하세요."
        _cmdb_build_q.put({"type": "error", "message": _cmdb_build_state["error"]})
        _cmdb_build_running = False
        return

    new_cache: dict = {}
    total_tasks = 0
    done_tasks = 0

    # 수집 대상 계산
    tasks = []
    for t in ALL_TYPES:
        rkey = RES_KEY_MAP.get(t, t)
        targets = [p for p in projects if (p.get("resources") or {}).get(rkey, 0) > 0]
        for p in targets:
            tasks.append((t, p["project_id"]))
    total_tasks = len(tasks)

    _cmdb_build_state["stage"] = f"총 {total_tasks}개 수집 시작"
    _cmdb_build_q.put({"type": "progress", "pct": 0, "stage": _cmdb_build_state["stage"], "done": 0, "total": total_tasks})

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_map = {ex.submit(get_resource_details, pid, t): (t, pid) for t, pid in tasks}
        for future in as_completed(future_map):
            t, pid = future_map[future]
            try:
                items = future.result()
            except Exception:
                items = []
            new_cache.setdefault(t, {})[pid] = items
            done_tasks += 1
            pct = int(done_tasks / total_tasks * 100) if total_tasks else 100
            _cmdb_build_state["pct"] = pct
            _cmdb_build_state["stage"] = f"{done_tasks}/{total_tasks} 완료"
            _cmdb_build_q.put({"type": "progress", "pct": pct, "stage": _cmdb_build_state["stage"],
                                "done": done_tasks, "total": total_tasks})

    _cmdb_detail_cache = new_cache
    _cmdb_detail_built_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    _cmdb_build_state["status"] = "done"
    CMDB_DETAIL_CACHE_FILE.write_text(
        json.dumps({"cache": new_cache, "built_at": _cmdb_detail_built_at}, ensure_ascii=False)
    )
    _cmdb_build_q.put({"type": "done"})
    _cmdb_build_running = False


@app.post("/api/gcp/cmdb/cache/build")
async def api_gcp_cmdb_cache_build(req: Request):
    """CMDB 상세 캐시 전체 빌드 (백그라운드)."""
    global _cmdb_build_running
    _check_admin(req)
    if _cmdb_build_running:
        return {"status": "already_running"}
    while not _cmdb_build_q.empty():
        _cmdb_build_q.get_nowait()
    threading.Thread(target=_run_cmdb_detail_build, daemon=True).start()
    return {"status": "started"}


@app.get("/api/gcp/cmdb/cache/stream")
async def api_gcp_cmdb_cache_stream():
    """CMDB 캐시 빌드 진행률 SSE."""
    async def gen():
        while True:
            try:
                msg = _cmdb_build_q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── GCP 빌링 ──────────────────────────────────────────────────────────
@app.get("/api/gcp/billing/settings")
async def api_gcp_billing_settings_get():
    return load_settings()


@app.post("/api/gcp/billing/settings")
async def api_gcp_billing_settings_save(request: Request):
    body = await request.json()
    save_settings({"bq_project": body.get("bq_project", ""), "bq_dataset": body.get("bq_dataset", "")})
    return {"status": "saved"}


def _run_billing_scan():
    global _billing_running
    _billing_running = True
    _billing_state["status"] = "scanning"
    _billing_state["error"] = None

    def on_progress(pct, stage):
        _billing_state["pct"] = pct
        _billing_state["stage"] = stage
        _billing_q.put({"type": "progress", "pct": pct, "stage": stage})

    try:
        s = load_settings()
        if s.get("bq_project") and s.get("bq_dataset"):
            costs = fetch_costs(s["bq_project"], s["bq_dataset"], on_progress)
            mode = "bigquery"
        else:
            on_progress(2, "빌링 계정 현황 + Recommender API 비용 추정 모드로 조회합니다...")
            # 프로젝트 스캔 데이터 활용 (있으면) — 없어도 빌링 계정 API에서 직접 수집
            projects = _state.get("projects") or []

            def _acct_prog(pct, stage):
                on_progress(2 + int(pct * 0.96), stage)
            costs = fetch_billing_accounts(projects, _acct_prog)
            mode = "account_overview"

        _billing_state["costs"] = costs
        _billing_state["mode"] = mode
        _billing_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _billing_state["status"] = "done"
        BILLING_COST_FILE.write_text(json.dumps({"costs": costs, "mode": mode, "last_scan": _billing_state["last_scan"]}, ensure_ascii=False))
        _billing_q.put({"type": "done", "mode": mode})
    except Exception as exc:
        _billing_state["status"] = "error"
        _billing_state["error"] = str(exc)
        _billing_q.put({"type": "error", "message": str(exc)})
    finally:
        _billing_running = False


@app.post("/api/gcp/billing/scan")
async def api_gcp_billing_scan_start():
    global _billing_running
    if _billing_running:
        return {"status": "already_running"}
    while not _billing_q.empty():
        try:
            _billing_q.get_nowait()
        except queue.Empty:
            break
    threading.Thread(target=_run_billing_scan, daemon=True).start()
    return {"status": "started"}


@app.get("/api/gcp/billing/stream")
async def api_gcp_billing_stream():
    async def generate():
        yield f"data: {json.dumps({'type': 'progress', 'pct': 0, 'stage': '시작 중...'})}\n\n"
        while True:
            try:
                event = _billing_q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if not _billing_running:
                    break
            await asyncio.sleep(0.4)
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/gcp/billing/costs")
async def api_gcp_billing_costs():
    return {
        "status":    _billing_state["status"],
        "pct":       _billing_state["pct"],
        "stage":     _billing_state["stage"],
        "last_scan": _billing_state["last_scan"],
        "error":     _billing_state["error"],
        "costs":     _billing_state["costs"],
        "mode":      _billing_state["mode"],
    }


# ── AWS 인메모리 캐시 ─────────────────────────────────────────────────
_aws_cache: dict = {
    "accounts":   {"data": None, "ts": 0.0},
    "org_tree":   {"data": None, "ts": 0.0},
    "org_ous":    {"data": None, "ts": 0.0},
    "costs":      {"data": None, "ts": 0.0},
}
_AWS_CACHE_TTL = 300.0   # 5분 (수동 새로고침 전까지 재사용)


def _aws_get(key: str):
    entry = _aws_cache.get(key, {})
    if entry.get("data") is not None and time.time() - entry.get("ts", 0) < _AWS_CACHE_TTL:
        return entry["data"]
    return None


def _aws_set(key: str, data):
    _aws_cache[key] = {"data": data, "ts": time.time()}
    return data


def _aws_bust(key: str = None):
    """key 지정 시 해당 캐시만, None 이면 전체 초기화."""
    if key:
        _aws_cache[key] = {"data": None, "ts": 0.0}
    else:
        for k in _aws_cache:
            _aws_cache[k] = {"data": None, "ts": 0.0}


# ── AWS API ───────────────────────────────────────────────────────────
@app.get("/api/aws/accounts")
async def api_aws_accounts(refresh: bool = False):
    if not refresh:
        cached = _aws_get("accounts")
        if cached is not None:
            return {**cached, "cached": True}
    try:
        accounts = list_accounts()
        result = {"accounts": accounts}
        _aws_set("accounts", result)
        return {**result, "cached": False}
    except Exception as e:
        return {"accounts": [], "error": str(e), "cached": False}


@app.get("/api/aws/resources/{account_id}")
async def api_aws_resources(account_id: str):
    try:
        return collect_account_resources(account_id)
    except Exception as e:
        return {"account_id": account_id, "error": str(e), "resources": []}


@app.get("/api/aws/costs/summary")
async def api_aws_costs_summary(
    refresh: bool = False,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    # 캐시 키를 월별로 분리 (start 기준)
    from datetime import date
    if not start:
        start = date.today().replace(day=1).isoformat()
    if not end:
        m = date.today()
        # 다음 달 1일
        if m.month == 12:
            end = f"{m.year + 1}-01-01"
        else:
            end = f"{m.year}-{str(m.month + 1).zfill(2)}-01"

    cache_key = f"costs_{start}"
    if not refresh:
        cached = _aws_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    try:
        acc_cached = _aws_get("accounts")
        if acc_cached:
            accounts = acc_cached["accounts"]
        else:
            accounts = list_accounts()
            _aws_set("accounts", {"accounts": accounts})
        account_ids = [a["id"] for a in accounts]
        summary = get_all_accounts_cost_summary(account_ids, start=start, end=end)
        acc_map = {a["id"]: a["name"] for a in accounts}
        for s in summary:
            s["name"] = acc_map.get(s["account_id"], s["account_id"])
        summary.sort(key=lambda x: x.get("cost", 0), reverse=True)
        result = {"summary": summary, "start": start, "end": end}
        _aws_set(cache_key, result)
        return {**result, "cached": False}
    except Exception as e:
        return {"summary": [], "error": str(e), "cached": False}


@app.get("/api/aws/costs/{account_id}")
async def api_aws_costs(account_id: str):
    try:
        return get_account_costs(account_id)
    except Exception as e:
        return {"account_id": account_id, "error": str(e), "periods": []}


@app.get("/api/aws/cmdb/summary")
async def api_aws_cmdb_summary():
    try:
        return {"summary": get_cmdb_summary()}
    except Exception as e:
        return {"summary": [], "error": str(e)}


@app.get("/api/aws/cmdb/account/{account_id}")
async def api_aws_cmdb_account(account_id: str, resource_type: Optional[str] = None):
    try:
        data = get_cmdb_account(account_id)
        if data is None:
            return {"account_id": account_id, "error": "계정을 찾을 수 없습니다"}
        if resource_type:
            return {"account_id": account_id, "resource_type": resource_type, "items": data.get(resource_type, [])}
        return data
    except Exception as e:
        return {"account_id": account_id, "error": str(e)}


# ── AWS VPC 온디맨드 캐시 ──────────────────────────────────────────────
_aws_vpc_cache: dict = {}   # {account_id: [vpc_items]}
_aws_vpc_built_at: Optional[str] = None

@app.get("/api/aws/vpc/list")
async def api_aws_vpc_list(account_id: Optional[str] = None):
    """계정별 VPC 목록. account_id 생략 시 캐시된 전체 반환."""
    if account_id:
        if account_id in _aws_vpc_cache:
            return {"account_id": account_id, "items": _aws_vpc_cache[account_id], "cached": True}
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, collect_account_resources, account_id
            )
            vpcs = [r for r in result.get("resources", []) if r.get("type") == "VPC"]
            _aws_vpc_cache[account_id] = vpcs
            return {"account_id": account_id, "items": vpcs, "cached": False}
        except Exception as e:
            return {"account_id": account_id, "items": [], "error": str(e)}
    # 전체 캐시 반환 (flat list with _account 필드)
    all_items = []
    for aid, items in _aws_vpc_cache.items():
        for item in items:
            all_items.append({**item, "_account": aid})
    return {"items": all_items, "built_at": _aws_vpc_built_at,
            "accounts": list(_aws_vpc_cache.keys())}


@app.post("/api/aws/vpc/build")
async def api_aws_vpc_build(req: Request):
    """전체 계정 VPC 캐시 빌드 (백그라운드 SSE)."""
    global _aws_vpc_built_at
    _check_admin(req)
    try:
        accounts_data = list_accounts()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    async def gen():
        total = len(accounts_data)
        done = 0
        for acc in accounts_data:
            aid = acc.get("account_id") or acc.get("id")
            if not aid:
                done += 1
                continue
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, collect_account_resources, aid
                )
                _aws_vpc_cache[aid] = [r for r in result.get("resources", []) if r.get("type") == "VPC"]
            except Exception:
                _aws_vpc_cache[aid] = []
            done += 1
            pct = int(done / total * 100) if total else 100
            yield f"data: {json.dumps({'type':'progress','pct':pct,'done':done,'total':total,'account':aid})}\n\n"
        _aws_vpc_built_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        yield f"data: {json.dumps({'type':'done','built_at':_aws_vpc_built_at})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/aws/cmdb/collect")
async def api_aws_cmdb_collect(_: dict = Depends(require_admin)):
    try:
        command_id = trigger_collection()
        return {"command_id": command_id, "status": "triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/aws/cmdb/collect/{command_id}")
async def api_aws_cmdb_collect_status(command_id: str):
    try:
        return get_collection_status(command_id)
    except Exception as e:
        return {"status": "Error", "stdout": "", "stderr": str(e)}


@app.post("/api/aws/cmdb/refresh")
async def api_aws_cmdb_refresh():
    try:
        data = fetch_cmdb_data(force=True)
        if data:
            _save_history("aws_cmdb", {"accounts": data, "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M")})
        return {"status": "refreshed", "count": len(data) if data else 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/dates")
async def api_history_dates():
    return {
        "gcp_projects":  _list_history_dates("gcp_projects"),
        "gcp_resources": _list_history_dates("gcp_resources"),
        "aws_cmdb":      _list_history_dates("aws_cmdb"),
    }

@app.get("/api/history/data")
async def api_history_data(kind: str, target_date: str):
    data = _load_history(kind, target_date)
    if data is None:
        raise HTTPException(status_code=404, detail="해당 날짜 데이터 없음")
    return data

@app.get("/api/aws/org/tree")
async def api_aws_org_tree(refresh: bool = False):
    if not refresh:
        cached = _aws_get("org_tree")
        if cached is not None:
            return {**cached, "cached": True}
    try:
        tree = get_ou_tree()
        _aws_set("org_tree", tree)
        return {**tree, "cached": False}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/aws/org/ous")
async def api_aws_org_ous(refresh: bool = False):
    if not refresh:
        cached = _aws_get("org_ous")
        if cached is not None:
            return {**cached, "cached": True}
    try:
        ous = get_all_ous_flat()
        result = {"ous": ous}
        _aws_set("org_ous", result)
        return {**result, "cached": False}
    except Exception as e:
        return {"ous": [], "error": str(e)}


@app.post("/api/aws/cache/refresh")
async def api_aws_cache_refresh(_: dict = Depends(require_admin)):
    """AWS 전체 캐시 초기화 — 다음 조회 시 실시간 수집."""
    _aws_bust()
    return {"ok": True, "message": "AWS 캐시 초기화 완료. 다음 탭 접근 시 새로 수집합니다."}


@app.post("/api/aws/org/move")
async def api_aws_org_move(request: Request, _: dict = Depends(require_admin)):
    try:
        body = await request.json()
        account_id = body.get("account_id")
        dest_parent_id = body.get("dest_parent_id")
        return_date = body.get("return_date")        # optional: YYYY-MM-DD
        return_ou_id = body.get("return_ou_id")      # optional: 복귀 OU ID
        if not account_id or not dest_parent_id:
            raise HTTPException(status_code=400, detail="account_id와 dest_parent_id가 필요합니다")
        parent = get_account_parent(account_id)
        source_parent_id = parent.get("id")
        if not source_parent_id:
            raise HTTPException(status_code=400, detail="현재 부모 OU를 찾을 수 없습니다")
        # 이미 목적지에 있거나 DuplicateAccountException → ok 처리
        already_there = (source_parent_id == dest_parent_id)
        if not already_there:
            try:
                move_account(account_id, source_parent_id, dest_parent_id)
            except Exception as e:
                err = str(e)
                if "DuplicateAccountException" in err or "already present" in err:
                    already_there = True
                else:
                    raise HTTPException(status_code=500, detail=err)
        result = {"ok": True, "already_there": already_there}
        # 이동 후 org 캐시 무효화
        _aws_bust("org_tree")
        _aws_bust("org_ous")
        # 복귀 예약 저장
        if return_date and return_ou_id:
            moves = _load_scheduled_moves()
            # 동일 계정 기존 예약 제거 후 새로 등록
            moves = [m for m in moves if m.get("account_id") != account_id]
            moves.append({
                "account_id": account_id,
                "dest_parent_id": dest_parent_id,
                "return_ou_id": return_ou_id,
                "return_date": return_date,
                "created_at": datetime.now().isoformat()
            })
            _save_scheduled_moves(moves)
            result["scheduled_return"] = return_date
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/aws/org/scheduled-moves")
async def api_get_scheduled_moves(_: dict = Depends(require_admin)):
    return {"moves": _load_scheduled_moves()}


@app.delete("/api/aws/org/scheduled-moves/{account_id}")
async def api_delete_scheduled_move(account_id: str, _: dict = Depends(require_admin)):
    moves = _load_scheduled_moves()
    new_moves = [m for m in moves if m.get("account_id") != account_id]
    _save_scheduled_moves(new_moves)
    return {"ok": True, "removed": len(moves) - len(new_moves)}


# ── 인증 정보 관리 ────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent / ".env"
GCP_SA_KEY_FILE = Path(__file__).parent / "gcp" / "service_account.json"


def _read_env() -> dict:
    """현재 .env 파일을 파싱해 dict 반환."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(env: dict) -> None:
    """dict를 .env 파일로 저장."""
    lines = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def _reload_aws_config(key_id: str, secret: str, region: str = "ap-northeast-2") -> None:
    """AWS 자격증명을 메모리 내 config 모듈에 즉시 반영."""
    import aws.services.aws_session as _sess
    _config.AWS_ACCESS_KEY_ID     = key_id
    _config.AWS_SECRET_ACCESS_KEY = secret
    _config.AWS_DEFAULT_REGION    = region
    # aws_session 모듈 변수도 갱신
    import importlib
    importlib.reload(_sess)


@app.get("/api/admin/credentials")
async def api_credentials_get(_: dict = Depends(require_admin)):
    """현재 저장된 인증 정보 반환 (Secret은 마스킹)."""
    env = _read_env()
    key_id = env.get("AWS_ACCESS_KEY_ID", "")
    secret = env.get("AWS_SECRET_ACCESS_KEY", "")

    # GCP: 서비스 계정 파일 존재 여부 / gcloud 토큰 상태 확인
    gcp_sa_exists = GCP_SA_KEY_FILE.exists()
    gcp_sa_email = ""
    if gcp_sa_exists:
        try:
            sa_data = json.loads(GCP_SA_KEY_FILE.read_text())
            gcp_sa_email = sa_data.get("client_email", "")
        except Exception:
            pass

    gcp_token_ok = False
    gcp_account = ""
    try:
        from gcp.gcp import get_auth_info
        info = get_auth_info()
        gcp_account  = info.get("account", "")
        gcp_token_ok = bool(gcp_account)
    except Exception:
        pass

    return {
        "aws": {
            "key_id":     key_id,
            "key_id_masked": (key_id[:4] + "****" + key_id[-4:]) if len(key_id) > 8 else "미설정",
            "secret_masked": ("*" * 8 + secret[-4:]) if len(secret) > 4 else "미설정",
            "region":     env.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
        },
        "gcp": {
            "mode":       "service_account" if gcp_sa_exists else "gcloud_token",
            "sa_email":   gcp_sa_email,
            "sa_file":    str(GCP_SA_KEY_FILE) if gcp_sa_exists else "",
            "gcloud_account": gcp_account,
            "token_ok":   gcp_token_ok,
        },
    }


@app.post("/api/admin/credentials/aws")
async def api_credentials_aws_save(request: Request, _: dict = Depends(require_admin)):
    """AWS Access Key / Secret Key 저장 후 즉시 적용."""
    body = await request.json()
    key_id = body.get("key_id", "").strip()
    secret = body.get("secret", "").strip()
    region = body.get("region", "ap-northeast-2").strip()

    if not key_id or not secret:
        raise HTTPException(status_code=400, detail="key_id와 secret은 필수입니다")
    if not key_id.startswith("AKIA") and not key_id.startswith("ASIA"):
        raise HTTPException(status_code=400, detail="올바른 AWS Access Key ID 형식이 아닙니다 (AKIA... 또는 ASIA...)")

    # .env 파일 업데이트
    env = _read_env()
    env["AWS_ACCESS_KEY_ID"]     = key_id
    env["AWS_SECRET_ACCESS_KEY"] = secret
    env["AWS_DEFAULT_REGION"]    = region
    _write_env(env)

    # 메모리 즉시 반영
    try:
        _reload_aws_config(key_id, secret, region)
        # 간단한 연결 테스트 (Organizations 조회)
        import boto3
        test_session = boto3.Session(
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name=region,
        )
        sts = test_session.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity.get("Account", "")
        return {"ok": True, "message": f"✅ 저장 완료 — 계정 ID: {account_id}"}
    except Exception as e:
        return {"ok": False, "message": f"저장은 됐지만 연결 테스트 실패: {e}"}


@app.post("/api/admin/credentials/gcp/service_account")
async def api_credentials_gcp_sa(request: Request, _: dict = Depends(require_admin)):
    """GCP 서비스 계정 JSON 키 저장 후 ADC로 설정."""
    body = await request.json()
    sa_json_str = body.get("json", "").strip()

    if not sa_json_str:
        raise HTTPException(status_code=400, detail="서비스 계정 JSON이 비어 있습니다")

    try:
        sa_data = json.loads(sa_json_str)
    except Exception:
        raise HTTPException(status_code=400, detail="JSON 형식이 올바르지 않습니다")

    required_fields = ["type", "project_id", "private_key", "client_email"]
    missing = [f for f in required_fields if f not in sa_data]
    if missing:
        raise HTTPException(status_code=400, detail=f"필수 필드 누락: {missing}")
    if sa_data.get("type") != "service_account":
        raise HTTPException(status_code=400, detail="서비스 계정 키 파일이 아닙니다 (type != service_account)")

    # 파일 저장
    GCP_SA_KEY_FILE.parent.mkdir(exist_ok=True)
    GCP_SA_KEY_FILE.write_text(sa_json_str)

    # 환경변수 설정 → gcp.py _get_credentials()가 우선 ADC로 탐지
    import os
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(GCP_SA_KEY_FILE)

    # gcp 모듈 credentials 캐시 초기화
    try:
        import gcp.gcp as _gcp_mod
        _gcp_mod._creds_cache = None  # 캐시 무효화
    except Exception:
        pass

    email = sa_data.get("client_email", "")
    project = sa_data.get("project_id", "")
    return {"ok": True, "message": f"✅ 서비스 계정 저장 완료\n이메일: {email}\n프로젝트: {project}"}


@app.post("/api/admin/credentials/gcp/token")
async def api_credentials_gcp_token(request: Request, _: dict = Depends(require_admin)):
    """GCP Access Token 직접 입력 — gcp.py의 OAuthCreds 캐시에 주입."""
    body = await request.json()
    token = body.get("token", "").strip()

    if not token:
        raise HTTPException(status_code=400, detail="토큰이 비어 있습니다")

    try:
        import gcp.gcp as _gcp_mod
        import google.oauth2.credentials as _gc

        creds = _gc.Credentials(
            token=token,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        # 캐시에 직접 주입 (TTL 50분 설정)
        _gcp_mod._creds_cache = creds
        _gcp_mod._creds_cache_ts = time.time()

        # 간단한 검증: 프로젝트 목록 1개 조회
        import google.auth.transport.requests as _gat
        from requests.adapters import HTTPAdapter
        session = _gat.AuthorizedSession(creds)
        session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4))
        r = session.get(
            "https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=1",
            timeout=10
        )
        if r.status_code == 401:
            _gcp_mod._creds_cache = None
            raise HTTPException(status_code=401, detail="토큰이 만료됐거나 유효하지 않습니다")
        r.raise_for_status()
        return {"ok": True, "message": "✅ Access Token 적용 완료 (유효 기간: ~1시간)"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"토큰 적용 실패: {e}")


@app.delete("/api/admin/credentials/gcp/service_account")
async def api_credentials_gcp_sa_delete(_: dict = Depends(require_admin)):
    """저장된 서비스 계정 키 파일 삭제."""
    if GCP_SA_KEY_FILE.exists():
        GCP_SA_KEY_FILE.unlink()
    import os
    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        import gcp.gcp as _gcp_mod
        _gcp_mod._creds_cache = None
    except Exception:
        pass
    return {"ok": True, "message": "서비스 계정 키 삭제 완료. gcloud 토큰 폴백으로 전환됩니다."}
