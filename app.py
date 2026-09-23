from pathlib import Path
from datetime import datetime, timezone
import json
import re
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from backend import run_travel_agent

# This is kept from the original project to allow the existing synchronous
# agent functions to call async MCP helpers inside FastAPI.
import nest_asyncio

nest_asyncio.apply()

BASE_DIR = Path(__file__).resolve().parent
PLANNER_BUILD = "tokyo-grounded-hitl-v7"

app = FastAPI(
    title="TripPilot AI",
    description="FastAPI and LangGraph travel planner with structured itineraries and resilient research fallbacks.",
    version="2.2.1",
)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _sanitize_public_data(value, field_name: str = ""):
    """Keep credentials and raw upstream exception details out of API JSON."""
    if isinstance(value, dict):
        return {key: _sanitize_public_data(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_public_data(item, field_name) for item in value]
    if not isinstance(value, str):
        return value

    value = re.sub(
        r"https?://[^\s\]\[()\"']*[?&]access_key=[^\s\]\[()\"']+",
        "[external API request details redacted]",
        value,
        flags=re.IGNORECASE,
    )
    lowered = value.lower()
    raw_error_markers = (
        "httpsconnectionpool", "connectionpool(", "traceback (most recent call last)",
        "runtimeerror:", "connectionerror(", "operationalerror:", "access_key=",
    )
    if any(marker in lowered for marker in raw_error_markers):
        if "weather" in field_name.lower():
            return "Live weather currently unavailable. Pack for seasonal averages."
        if "flight" in field_name.lower():
            return "Live flight details offline. Standard carriers: Biman Bangladesh, IndiGo, Air India"
        return "Live external research is currently unavailable. Verify details directly with the provider."
    return value


class TravelRequest(BaseModel):
    message: str
    thread_id: str | None = None


class ApprovalRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    approved: bool
    feedback: str = ""


class FeedbackRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=200)
    rating: int = Field(ge=1, le=5)
    feedback: str = Field(min_length=1, max_length=2000)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/travel")
async def travel_planner(request_data: TravelRequest):
    try:
        user_message = request_data.message.strip()

        if not user_message:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": "Message cannot be empty.",
                },
            )

        result = run_travel_agent(
            user_input=user_message,
            thread_id=request_data.thread_id,
        )

        return JSONResponse(
            content={
                "success": True,
                "build_id": PLANNER_BUILD,
                **_sanitize_public_data(result),
            }
        )

    except Exception:
        print("Travel request failed; details were suppressed.", flush=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "We could not generate the travel plan right now. Please try again.",
            },
        )


@app.post("/api/travel/approve")
async def approve_travel_plan(request_data: ApprovalRequest):
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "error": "This version of the travel planner does not have an approval step. Please create a plan with /api/travel.",
        },
    )


@app.post("/api/feedback")
async def submit_feedback(request_data: FeedbackRequest):
    """Save final-plan feedback locally for human review."""
    record = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "thread_id": request_data.thread_id,
        "rating": request_data.rating,
        "feedback": request_data.feedback.strip(),
    }
    try:
        with (BASE_DIR / "feedback.jsonl").open("a", encoding="utf-8") as feedback_file:
            feedback_file.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Feedback could not be saved. Please try again."},
        )
    return {"success": True, "message": "Thank you. Your feedback was saved for review."}


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "message": "TripPilot AI API is running",
        "version": "2.2.1",
        "planner_build": PLANNER_BUILD,
        "features": [
            "structured_day_by_day_itineraries",
            "destination_grounded_fallbacks",
            "sanitized_external_api_errors",
            "origin_currency_budget_estimates",
        ],
    }


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(content={})


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8001,
        reload=True,
    )
