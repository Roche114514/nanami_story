"""七海千秋 · 故事引擎（nanami_story）：以「命运丝线」为唯一核心引擎的独立插件。"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from sdk.plugin import PluginBase

from . import PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION, story_page
from .adapters.background_adapter import BackgroundAdapter
from .adapters.host_chat_adapter import HostChatAdapter
from .adapters.persona_adapter import PersonaAdapter
from .adapters.world_adapter import WorldAdapter
from .dialogue_injector import DialogueInjector
from .fate_runtime import FateRuntime
from .mouth_rewriter import DialogueMouthRewriter, StoryMouthDialogHandler
from .settings import SETTINGS_FILENAME, SettingsStore

logger = logging.getLogger(__name__)


class NanamiStoryPlugin(PluginBase):
    """Shinsekai 独立故事引擎插件入口（以命运丝线 Fate Weaver 为核心）。"""

    def __init__(self) -> None:
        self._code_dir: Path | None = None
        self._data_root: Path | None = None
        self._settings_store: SettingsStore | None = None
        self._world_adapter: WorldAdapter | None = None
        self._persona_adapter: PersonaAdapter | None = None
        self._background_adapter: BackgroundAdapter | None = None
        self._fate_runtime: FateRuntime | None = None
        self._host_chat_adapter: HostChatAdapter | None = None
        self._stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    @property
    def plugin_version(self) -> str:
        return PLUGIN_VERSION

    @property
    def plugin_name(self) -> str:
        return PLUGIN_NAME

    @property
    def plugin_description(self) -> str:
        return PLUGIN_DESCRIPTION

    @property
    def plugin_author(self) -> str:
        return PLUGIN_AUTHOR

    @property
    def priority(self) -> int:
        return 35

    @property
    def fate_runtime(self) -> FateRuntime | None:
        return self._fate_runtime

    def initialize(self, register, plugin_root: Path, host) -> None:
        _ = plugin_root
        code_dir = Path(__file__).resolve().parent
        project_root = Path.cwd().resolve()
        raw_data_dir = Path(getattr(host, "project_data_dir", None) or "data")
        if not raw_data_dir.is_absolute():
            raw_data_dir = (project_root / raw_data_dir).resolve()
        data_root = raw_data_dir / "plugins" / PLUGIN_ID

        self._code_dir = code_dir
        self._data_root = data_root
        self._settings_store = SettingsStore(data_root / SETTINGS_FILENAME)
        settings = self._settings_store.get()

        self._world_adapter = WorldAdapter()
        self._persona_adapter = PersonaAdapter(
            project_root,
            custom_persona_dir=settings.persona_data_dir,
        )
        self._background_adapter = BackgroundAdapter(project_root)
        self._fate_runtime = FateRuntime(
            self._world_adapter,
            self._persona_adapter,
            data_root,
            settings_provider=self._settings_store.get,
            background_adapter=self._background_adapter,
        )
        owner = (os.environ.get("SHINSEKAI_MEMORY_SERVICE_OWNER") == "1"
                 and "SHINSEKAI_CHAT_LAUNCH_CONFIG" not in os.environ)
        self._fate_runtime.restore_sessions(restore_world=owner)

        submit = None
        if hasattr(register, "frontend_user_input"):
            try:
                submit = register.frontend_user_input().submit_text
            except Exception:
                logger.debug("nanami_story: 聊天输入通道不可用", exc_info=True)
        host_chat = HostChatAdapter(submit)
        self._host_chat_adapter = host_chat
        settings_provider = self._settings_store.get
        rewriter = DialogueMouthRewriter(self._persona_adapter, settings_provider)
        injector = DialogueInjector(self._fate_runtime, settings_provider, mouth_rewriter=rewriter)
        if hasattr(register, "register_init_chat_hook"):
            register.register_init_chat_hook(injector.init_chat, label="命运剧本")
        if hasattr(register, "register_before_chat_hook"):
            register.register_before_chat_hook(injector.before_chat)
        if hasattr(register, "register_message_added_hook"):
            register.register_message_added_hook(injector.message_added)
        if hasattr(register, "register_message_handler"):
            mouth_handler = StoryMouthDialogHandler(
                injector=injector, rewriter=rewriter, settings_provider=settings_provider,
            )
            register.register_message_handler(dialog_media_handler=mouth_handler)

        story_page.register_pages(
            register,
            code_dir=code_dir,
            fate_runtime=self._fate_runtime,
            world_adapter=self._world_adapter,
            persona_adapter=self._persona_adapter,
            settings_store=self._settings_store,
            host_chat_adapter=host_chat,
            background_adapter=self._background_adapter,
        )

        if owner:
            self._start_poll_loop()
        logger.info("nanami_story: 命运丝线引擎初始化完成（data=%s）", data_root)

    def _start_poll_loop(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                interval = 1.5
                try:
                    if self._settings_store is not None:
                        cfg = self._settings_store.get()
                        interval = max(0.5, float(cfg.poll_interval_sec))
                        if cfg.enabled and self._world_adapter is not None and self._world_adapter.available():
                            if self._fate_runtime is not None:
                                self._fate_runtime.tick_all()
                        if cfg.enabled and self._fate_runtime is not None and self._host_chat_adapter is not None:
                            self._fate_runtime.tick_observer_autoplay(self._host_chat_adapter)
                except Exception:
                    logger.debug("nanami_story: 命运丝线后台轮询周期异常", exc_info=True)
                self._stop_event.wait(interval)

        self._poll_thread = threading.Thread(
            target=_loop,
            name="nanami-story-fate-weaver",
            daemon=True,
        )
        self._poll_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)
        self._poll_thread = None
        if self._fate_runtime is not None:
            self._fate_runtime.close()
