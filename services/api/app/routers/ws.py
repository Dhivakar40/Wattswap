"""The single WebSocket endpoint. All behaviour lives in `app.realtime.gateway`."""

from fastapi import APIRouter, WebSocket

from app.realtime.gateway import Gateway

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    gateway: Gateway = ws.app.state.gateway
    await gateway.serve(ws)
