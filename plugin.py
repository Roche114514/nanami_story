"""七海千秋 · 故事引擎（nanami_story）：以「命运丝线」为唯一核心引擎的独立插件。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from sdk.plugin import PluginBase

from . import PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION, story_page
from .adapters.persona_adapter import PersonaAdapter
from .adapters.world_adapter import WorldAdapter
from .fate_runtime import FateRuntime
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
        self._fate_runtime: FateRuntime | None = None
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
        return 70

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
        self._fate_runtime = FateRuntime(
            self._world_adapter,
            self._persona_adapter,
            data_root,
            settings_provider=self._settings_store.get,
        )
        self._fate_runtime.restore_sessions()

        story_page.register_pages(
            register,
            code_dir=code_dir,
            fate_runtime=self._fate_runtime,
            world_adapter=self._world_adapter,
            persona_adapter=self._persona_adapter,
            settings_store=self._settings_store,
        )

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
