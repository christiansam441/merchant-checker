from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.checklist import ChecklistRules, load_checklist_rules
from app.limits import ConcurrencyGate, SlidingWindowRateLimiter
from app.rules import DetectionRules, load_rules
from app.scanner import scan_events


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
SCANS_PER_MINUTE = 10
MAX_CONCURRENT_SCANS = 4


class ScanRequest(BaseModel):
    url: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.detection_rules = load_rules()
    app.state.checklist_rules = load_checklist_rules()
    app.state.rate_limiter = SlidingWindowRateLimiter(SCANS_PER_MINUTE, 60)
    app.state.scan_gate = ConcurrencyGate(MAX_CONCURRENT_SCANS)
    yield


app = FastAPI(title="Merchant Checker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str | int]:
    rules: DetectionRules = app.state.detection_rules
    checklist_rules: ChecklistRules = app.state.checklist_rules
    return {
        "status": "ok",
        "processor_rules": len(rules.processors),
        "checklist_rules": len(checklist_rules.checklist_items),
    }


@app.post("/api/scan")
async def scan(scan_request: ScanRequest, request: Request) -> Response:
    rules: DetectionRules = app.state.detection_rules
    checklist_rules: ChecklistRules = app.state.checklist_rules
    client_ip = request.client.host if request.client else "unknown"
    rate_limiter: SlidingWindowRateLimiter = app.state.rate_limiter
    scan_gate: ConcurrencyGate = app.state.scan_gate

    if not await rate_limiter.allow(client_ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Scan limit reached. Try again in one minute."},
        )
    if not await scan_gate.try_acquire():
        return JSONResponse(
            status_code=429,
            content={"detail": "The scanner is busy. Try again shortly."},
        )

    async def event_stream():
        try:
            async for event in scan_events(scan_request.url, rules, checklist_rules):
                yield f"data: {event.model_dump_json()}\n\n"
        finally:
            await scan_gate.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
