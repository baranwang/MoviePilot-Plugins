# plugins.v2/transferfaildelete/__init__.py
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import traceback

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.chain.storage import StorageChain
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


class TransferFailDelete(_PluginBase):
    # 插件名称
    plugin_name = "整理失败与重复下载清理"
    # 插件描述
    plugin_desc = "媒体整理失败时自动删除源文件，并定期清理已整理过但重复下载残留的本地文件。"
    # 插件图标
    plugin_icon = "Eraser.png"
    # 插件版本
    plugin_version = "1.1.0"
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
    _cron: str = "0 */2 * * *"
    _clean_repeat: bool = True
    _retain_hours: int = 6

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._cron = config.get("cron") or "0 */2 * * *"
            self._clean_repeat = config.get("clean_repeat", True)
            self._retain_hours = int(config.get("retain_hours") or 6)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._clean_repeat and self._cron:
            return [
                {
                    "id": "TransferFailDeleteRepeatCleanup",
                    "name": "重复下载残留清理",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.cleanup_repeat_sources,
                    "kwargs": {},
                }
            ]
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_repeat",
                                            "label": "清理重复下载残留",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "巡检周期",
                                            "placeholder": "5 位 cron 表达式",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retain_hours",
                                            "label": "保留小时数",
                                            "type": "number",
                                            "min": 1,
                                            "placeholder": "文件至少保留多少小时后再清理",
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
                                                "同时可按周期巡检本地下载目录，清理已整理成功但因重复下载再次出现的残留文件。"
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
            "cron": "0 */2 * * *",
            "clean_repeat": True,
            "retain_hours": 6,
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
        fail_reason = getattr(transferinfo, "message", None) or "未知"

        logger.info(f"整理失败，准备删除源文件：{fileitem.path}，原因：{fail_reason}")
        self._delete_source_file(
            fileitem=fileitem,
            download_hash=download_hash,
            reason=f"整理失败：{fail_reason}",
        )

    def cleanup_repeat_sources(self):
        """
        定期清理已整理过但重复下载残留的本地源文件
        """
        if not self._enabled or not self._clean_repeat:
            return

        cutoff_time = datetime.now() - timedelta(hours=max(self._retain_hours, 1))
        media_exts = {
            ext.lower()
            for ext in (
                settings.RMT_MEDIAEXT
                + settings.RMT_SUBEXT
                + settings.RMT_AUDIOEXT
            )
        }
        storage_chain = StorageChain()
        transferhis = TransferHistoryOper()
        downloadhis = DownloadHistoryOper()
        cleaned_paths: List[str] = []
        failed_paths: List[str] = []

        for directory in DirectoryHelper().get_local_download_dirs():
            download_path = getattr(directory, "download_path", None)
            if not download_path:
                continue

            base_path = Path(download_path)
            if not base_path.exists():
                logger.debug(f"重复下载巡检跳过不存在的目录：{base_path}")
                continue

            for file_path in base_path.rglob("*"):
                try:
                    if not file_path.is_file():
                        continue
                    if file_path.suffix.lower() not in media_exts:
                        continue
                    if datetime.fromtimestamp(file_path.stat().st_mtime) > cutoff_time:
                        continue

                    transfer_history = transferhis.get_by_src(
                        file_path.as_posix(),
                        storage="local",
                    )
                    if not transfer_history or not transfer_history.status:
                        continue
                    if not self._dest_exists(storage_chain, transfer_history):
                        continue

                    fileitem = storage_chain.get_file_item(
                        storage="local",
                        path=file_path,
                    )
                    if not fileitem:
                        continue

                    download_hash = downloadhis.get_hash_by_fullpath(file_path.as_posix())
                    if self._delete_source_file(
                        fileitem=fileitem,
                        download_hash=download_hash,
                        reason="已整理成功的重复下载残留",
                        send_notify=False,
                    ):
                        cleaned_paths.append(file_path.as_posix())
                    else:
                        failed_paths.append(file_path.as_posix())
                except Exception as e:
                    logger.error(
                        f"巡检重复下载残留失败：{file_path}，{e}\n{traceback.format_exc()}"
                    )
                    failed_paths.append(file_path.as_posix())

        if cleaned_paths:
            logger.info(f"重复下载残留清理完成，共清理 {len(cleaned_paths)} 个文件")
            if self._notify:
                preview = "\n".join(cleaned_paths[:10])
                more_text = ""
                if len(cleaned_paths) > 10:
                    more_text = f"\n... 另有 {len(cleaned_paths) - 10} 个文件"
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="重复下载残留清理完成",
                    text=f"共清理 {len(cleaned_paths)} 个文件：\n{preview}{more_text}",
                )

        if failed_paths:
            logger.warning(f"重复下载残留清理失败，共 {len(failed_paths)} 个文件待人工检查")

    @staticmethod
    def _dest_exists(storage_chain: StorageChain, transfer_history) -> bool:
        """
        仅在目标文件仍然存在时，才删除重复下载残留
        """
        dest_storage = getattr(transfer_history, "dest_storage", None)
        dest = getattr(transfer_history, "dest", None)
        if not dest_storage or not dest:
            return False
        return bool(storage_chain.get_file_item(storage=dest_storage, path=Path(dest)))

    def _delete_source_file(
        self,
        fileitem,
        download_hash: Optional[str],
        reason: str,
        send_notify: bool = True,
    ) -> bool:
        """
        删除源文件，并同步清理下载记录 / 下载任务
        """
        result = StorageChain().delete_media_file(fileitem)

        if result:
            logger.info(f"源文件已删除：{fileitem.path}，原因：{reason}")
            try:
                DownloadHistoryOper().delete_file_by_fullpath(fileitem.path)
                if download_hash:
                    eventmanager.send_event(
                        EventType.DownloadFileDeleted,
                        {"src": fileitem.path, "hash": download_hash},
                    )
            except Exception as e:
                logger.error(f"清理下载记录失败：{e}")

            if self._notify and send_notify:
                self.post_message(
                    mtype=NotificationType.Manual,
                    title="源文件已删除",
                    text=f"文件：{fileitem.path}\n原因：{reason}",
                )
            return True

        logger.error(f"源文件删除失败：{fileitem.path}，原因：{reason}")
        if self._notify and send_notify:
            self.post_message(
                mtype=NotificationType.Manual,
                title="源文件删除失败",
                text=f"文件：{fileitem.path}\n原因：{reason}\n请手动处理",
            )
        return False

    def stop_service(self):
        pass
