"""宿主聊天输入适配器（预留形态 1/2/3 联动接口，保持完全解耦）。"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


class HostChatAdapter:
    """封装向宿主聊天窗口发送消息的可选通道。"""

    def __init__(self, submit_fn: Callable[[str], Any] | None = None) -> None:
        self._submit_fn = submit_fn

    def available(self) -> bool:
        return callable(self._submit_fn)

    def submit_text(self, text: str) -> bool:
        if not callable(self._submit_fn):
            return False
        clean = str(text or "").strip()
        if not clean:
            return False
        try:
            res = self._submit_fn(clean)
            return True if res is None else bool(res)
        except Exception:
            logger.debug("nanami_story: HostChatAdapter submit_text 跳过", exc_info=True)
            return False
