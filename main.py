# main.py
import asyncio
import json
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends, status
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from reporter import ReportEngine

BASE = Path(__file__).parent
STATIC = BASE / "static"

# ==========================================
# KONFIGURASI LOGIN & KEAMANAN
# ==========================================
ALLOWED_EMAIL = "genshinimpectc5@gmail.com"
DEFAULT_PASSWORD = "GALAK2026!"
SECRET_KEY = "galak-super-secret-key-ganti-kalo-perlu"

app = FastAPI(title="GALAK · WA Mass Reporter")

# 1. TAMBAH CORS DULUAN (Wajib paling atas biar Vercel bisa akses)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.vercel\.app", # Izinkan semua domain Vercel
    allow_credentials=True, # Izinkan cookie session lewat
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. BARU TAMBAH SESSION (Buat sistem login)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

# Mount static (buat akses UI lokal di VPS kalau mau)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class JobConfig(BaseModel):
    target: str
    total_reports: int = 5000
    reports_per_account: int = 50
    report_delay_min: int = 30
    report_delay_max: int = 120
    account_delay_min: int = 60
    account_delay_max: int = 180
    proxies: str = ""
    accounts: str = ""
    headless: bool = True


class AppState:
    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()
        self.clients: List[WebSocket] = []
        self.stats: dict = {}
        self.running: bool = False

    async def broadcast(self, type_: str, data):
        msg = json.dumps({"type": type_, "data": data})
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.clients:
                self.clients.remove(ws)

state = AppState()


# ==========================================
# MIDDLEWARE AUTH
# ==========================================
def verify_auth(request: Request):
    user = request.session.get("user_email")
    if user != ALLOWED_EMAIL:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return user


# ==========================================
# ROUTES
# ==========================================
@app.get("/")
async def index(request: Request):
    if request.session.get("user_email") != ALLOWED_EMAIL:
        return RedirectResponse(url="/login")
    return FileResponse(STATIC / "index.html")

@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user_email") == ALLOWED_EMAIL:
        return RedirectResponse(url="/")
    return FileResponse(STATIC / "login.html")

@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    
    if email == ALLOWED_EMAIL.lower() and password == DEFAULT_PASSWORD:
        request.session["user_email"] = ALLOWED_EMAIL
        return {"ok": True}
    return JSONResponse(status_code=401, content={"ok": False, "error": "Email atau Password salah."})

@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/stats")
async def api_stats(user: str = Depends(verify_auth)):
    return {"running": state.running, "stats": state.stats}

@app.post("/api/start")
async def api_start(cfg: JobConfig, user: str = Depends(verify_auth)):
    if state.running:
        return {"ok": False, "error": "Job sedang berjalan"}

    state.stop_event = asyncio.Event()
    state.running = True
    state.stats = {}

    async def log_cb(level: str, msg: str):
        if level == "STATS":
            try:
                state.stats = json.loads(msg)
            except Exception:
                state.stats = {}
            await state.broadcast("stats", state.stats)
        else:
            await state.broadcast("log", {"level": level, "msg": msg})

    engine = ReportEngine(cfg.model_dump(), log_cb, state.stop_event)

    async def runner():
        try:
            await engine.run()
        except Exception as e:
            await log_cb("ERROR", f"Engine crash: {e}")
        finally:
            state.running = False
            await state.broadcast("done", {"stats": engine.stats})

    state.task = asyncio.create_task(runner())
    return {"ok": True}

@app.post("/api/stop")
async def api_stop(user: str = Depends(verify_auth)):
    if not state.running:
        return {"ok": False, "error": "Tidak ada job"}
    state.stop_event.set()
    return {"ok": True}

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    user = ws.session.get("user_email")
    if user != ALLOWED_EMAIL:
        await ws.close(code=1008) # Close dengan kode Policy Violation
        return
        
    await ws.accept()
    state.clients.append(ws)
    try:
        await ws.send_text(json.dumps({"type": "stats", "data": state.stats}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if ws in state.clients:
            state.clients.remove(ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
