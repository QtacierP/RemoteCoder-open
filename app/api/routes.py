"""FastAPI routes for health and admin operations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.schemas import ChatResponse, HealthResponse, SessionStatusResponse
from app.services.session_service import SessionService

router = APIRouter()


def get_session_service(request: Request) -> SessionService:
    return request.app.state.session_service


@router.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "Telegram Codex Bridge",
        "status": "ok",
        "health": "/health",
        "telegram_webhook": "/telegram/webhook",
    }


@router.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", telegram_mode=request.app.state.settings.telegram_mode)


@router.get("/sessions")
async def list_sessions(service: SessionService = Depends(get_session_service)) -> list[dict]:
    return service.list_sessions()


@router.get("/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session(session_id: str, service: SessionService = Depends(get_session_service)) -> SessionStatusResponse:
    try:
        session = service.get_session_status(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Session not found") from None
    backend_status = session.pop("backend_status")
    return SessionStatusResponse(session=session, backend_status=backend_status)


@router.post("/sessions/{session_id}/reset")
async def reset_session(session_id: str, service: SessionService = Depends(get_session_service)) -> dict:
    status = service.get_session_status(session_id)
    chat_id = status["chat_id"]
    new_session = service.reset_chat_session(chat_id)
    return {"ok": True, "old_session_id": session_id, "new_session_id": new_session["session_id"]}


@router.get("/chats/{chat_id}", response_model=ChatResponse)
async def get_chat(chat_id: int, service: SessionService = Depends(get_session_service)) -> ChatResponse:
    session = service.get_chat(chat_id)
    return ChatResponse(chat_id=chat_id, session=session)


@router.post("/telegram/webhook")
async def telegram_webhook(payload: dict, request: Request) -> dict:
    processor = request.app.state.telegram_update_processor
    await processor([payload], source="webhook")
    return {"ok": True}
