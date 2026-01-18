from typing import Any
import os
import asyncio
import platform
from pathlib import Path

from src.constants.constants import AbortReason
from src.plugins.base import Plugin
from src.utils.logging_config import get_logger
from src.utils.config_manager import ConfigManager


logger = get_logger(__name__)


class WakeWordPlugin(Plugin):
    name = "wake_word"
    priority = 30  # 依赖 AudioPlugin

    def __init__(self) -> None:
        super().__init__()
        self.app = None
        self.detector = None

    async def setup(self, app: Any) -> None:
        self.app = app
        try:
            from src.audio_processing.wake_word_detect import WakeWordDetector

            self.detector = WakeWordDetector()
            if not getattr(self.detector, "enabled", False):
                self.detector = None
                return

            # 绑定回调
            self.detector.on_detected(self._on_detected)
        except ImportError as e:
            logger.error(f"无法导入唤醒词检测器: {e}")
            self.detector = None
        except Exception as e:
            logger.error(f"唤醒词插件初始化失败: {e}", exc_info=True)
            self.detector = None

    async def start(self) -> None:
        if not self.detector:
            return
        try:
            # 需要音频编码器以提供原始PCM数据
            audio_codec = getattr(self.app, "audio_codec", None)
            if audio_codec is None:
                logger.warning("未找到audio_codec，无法启动唤醒词检测")
                return
            await self.detector.start(audio_codec)
        except Exception as e:
            logger.error(f"启动唤醒词检测器失败: {e}", exc_info=True)

    async def stop(self) -> None:
        if self.detector:
            try:
                await self.detector.stop()
            except Exception as e:
                logger.warning(f"停止唤醒词检测器失败: {e}")

    async def shutdown(self) -> None:
        if self.detector:
            try:
                await self.detector.stop()
            except Exception as e:
                logger.warning(f"关闭唤醒词检测器失败: {e}")

    async def _on_detected(self, wake_word, full_text):
        # 检测到唤醒词：切到自动对话（根据 AEC 自动选择实时/自动停）
        try:
            # 若正在说话，交给应用的打断/状态机处理
            if hasattr(self.app, "device_state") and hasattr(
                self.app, "start_auto_conversation"
            ):
                if self.app.is_speaking():
                    await self.app.abort_speaking(AbortReason.WAKE_WORD_DETECTED)
                    audio_plugin = self.app.plugins.get_plugin("audio")
                    if audio_plugin and audio_plugin.codec:
                        await audio_plugin.codec.clear_audio_queue()
                else:
                    await self.app.start_auto_conversation()
        except Exception as e:
            logger.error(f"处理唤醒词检测失败: {e}", exc_info=True)

        # 根据配置播放对应的本地音频（默认异步播放）
        try:
            config = ConfigManager.get_instance()
            audio_map = config.get_config("WAKE_WORD_OPTIONS.ALERT_AUDIO_MAP", {}) or {}
            blocking_default = config.get_config("WAKE_WORD_OPTIONS.ALERT_AUDIO_BLOCKING", False)

            if audio_map:
                # 尝试从 wake_word 或 full_text 中匹配 key（优先精确匹配，再子串匹配）
                detected = ""
                try:
                    detected = str(wake_word) if wake_word is not None else ""
                except Exception:
                    detected = ""

                selected_path = None
                # 精确匹配优先
                for key, path in audio_map.items():
                    if key is None:
                        continue
                    try:
                        if detected == str(key):
                            selected_path = path
                            break
                    except Exception:
                        continue

                # 子串匹配
                if selected_path is None:
                    for key, path in audio_map.items():
                        if key is None:
                            continue
                        try:
                            if key in detected:
                                selected_path = path
                                break
                            if full_text and key in str(full_text):
                                selected_path = path
                                break
                        except Exception:
                            continue

                if selected_path:
                    # 异步播放（默认）——在线程池中执行阻塞播放代码
                    loop = asyncio.get_running_loop()
                    if blocking_default:
                        await loop.run_in_executor(None, lambda: _play_audio_file(selected_path, True))
                    else:
                        # 不等待播放完成
                        loop.run_in_executor(None, lambda: _play_audio_file(selected_path, False))
        except Exception as e:
            logger.error(f"唤醒词播放提示音失败: {e}", exc_info=True)


def _play_audio_file(filepath: str, blocking: bool = False):
    """
    播放本地音频文件（优先在 Windows 使用 winsound 播放 WAV）。
    - filepath: 本地文件路径（相对路径相对于项目根）
    - blocking: 如果 True，在当前线程等待播放完成；否则异步返回
    说明：winsound 仅支持 WAV。non-Windows 环境尝试 simpleaudio (wav) 作为回退。
    """
    try:
        if not filepath:
            return

        p = Path(os.path.expanduser(filepath))
        if not p.is_absolute():
            # 以项目根为基准解析相对路径（项目结构： src/... -> parents[2] 为 repo root）
            project_root = Path(__file__).resolve().parents[2]
            p = (project_root / p).resolve()

        if not p.exists():
            logger.warning(f"唤醒提示音文件不存在: {p}")
            return

        # Windows 优先使用 winsound（仅支持 wav）
        if os.name == "nt":
            try:
                import winsound

                flags = winsound.SND_FILENAME
                flags |= winsound.SND_ASYNC if not blocking else winsound.SND_SYNC
                winsound.PlaySound(str(p), flags)
                return
            except Exception as e:
                logger.warning(f"winsound 播放失败，尝试回退播放: {e}")

        # 非 Windows 或 winsound 失败 -> 尝试 simpleaudio（需 pip install simpleaudio）
        try:
            import simpleaudio as sa

            wave_obj = sa.WaveObject.from_wave_file(str(p))
            play_obj = wave_obj.play()
            if blocking:
                play_obj.wait_done()
            return
        except Exception as e:
            logger.warning(f"simpleaudio 播放失败或未安装: {e}")

        logger.warning("未能播放提示音（无可用播放后备方案）")
    except Exception as e:
        logger.error(f"播放文件出错: {e}", exc_info=True)
    def _on_error(self, error):
        try:
            logger.error(f"唤醒词检测错误: {error}")
            if hasattr(self.app, "set_chat_message"):
                self.app.set_chat_message("assistant", f"[唤醒词错误] {error}")
        except Exception as e:
            logger.error(f"处理唤醒词错误回调失败: {e}")
