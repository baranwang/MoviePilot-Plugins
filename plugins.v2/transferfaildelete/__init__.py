# plugins.v2/transferfaildelete/__init__.py
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.chain.storage import StorageChain
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class TransferFailDelete(_PluginBase):
    # 插件名称
    plugin_name = "整理失败源文件清理"
    # 插件描述
    plugin_desc = "媒体整理失败时自动删除源文件，与手动「删除转移记录和源文件」逻辑一致。"
    # 插件图标
    plugin_icon = "Eraser.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "baranwang"
    # 作者主页
    author_url = "https://github.com/baranwang"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "transferfaildelete_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 2

    # 配置属性
    _enabled: bool = False
    _notify: bool = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": (
                                                "启用后，媒体整理失败时将自动删除源文件（不可恢复）。\n\n"
                                                "整理失败历史记录不会被删除，仍可在整理历史中查看。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        return []

    @eventmanager.register(EventType.TransferFailed)
    def on_transfer_failed(self, event: Event):
        """
        整理失败时删除源文件
        """
        if not self._enabled:
            return

        event_data = event.event_data or {}
        fileitem = event_data.get("fileitem")
        if not fileitem:
            logger.warning("整理失败事件缺少 fileitem，跳过源文件清理")
            return

        transferinfo = event_data.get("transferinfo")
        download_hash = event_data.get("download_hash")
        fail_reason = transferinfo.message if transferinfo else "未知"

        logger.info(f"整理失败，准备删除源文件：{fileitem.path}，原因：{fail_reason}")

        result = StorageChain().delete_media_file(fileitem)

        if result:
            logger.info(f"源文件已删除：{fileitem.path}")

            # 清理下载记录
            if download_hash:
                try:
                    DownloadHistoryOper().delete_file_by_fullpath(
                        Path(fileitem.path).as_posix()
                    )
                    eventmanager.send_event(
                        EventType.DownloadFileDeleted,
                        {"src": fileitem.path, "hash": download_hash},
                    )
                except Exception as e:
                    logger.error(f"清理下载记录失败：{e}")

            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="整理失败源文件已删除",
                    text=f"文件：{fileitem.path}\n原因：{fail_reason}",
                )
        else:
            logger.error(f"源文件删除失败：{fileitem.path}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="整理失败源文件删除失败",
                    text=f"文件：{fileitem.path}\n请手动处理",
                )

    def stop_service(self):
        pass
