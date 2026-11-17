"""领域层：不依赖 Qt / HTTP / SQL。"""
from .states import ParseResult, RoomState, classify_response, KNOWN_STATUS_MAP
from .models import Room

__all__ = [
    "ParseResult",
    "RoomState",
    "classify_response",
    "KNOWN_STATUS_MAP",
    "Room",
]
