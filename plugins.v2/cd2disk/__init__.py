from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.helper.storage import StorageHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import FileItem, StorageOperSelectionEventData, StorageUsage
from app.schemas.types import ChainEventType

from .cd2_api import Cd2Api


class Cd2Disk(_PluginBase):
    # 插件名称
    plugin_name = "CloudDrive2 储存"
    # 插件描述
    plugin_desc = "对接 CloudDrive2，提供 MoviePilot 储存模块能力。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    # 插件版本
    plugin_version = "0.1.1"
    # 插件作者
    plugin_author = "baranwang"
    # 作者主页
    author_url = "https://github.com/baranwang"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "cd2disk_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    _enabled = False
    _disk_name = "CloudDrive2"
    _cd2_api: Optional[Cd2Api] = None
    _cd2_url = None
    _cd2_api_key = None

    def __init__(self):
        super().__init__()
        self._disk_name = "CloudDrive2"

    def init_plugin(self, config: Optional[dict] = None):
        """
        初始化插件
        """
        self.stop_service()

        if not config:
            return

        storage_helper = StorageHelper()
        storages = storage_helper.get_storagies()
        if not any(s.type == self._disk_name and s.name == self._disk_name for s in storages):
            storage_helper.add_storage(storage=self._disk_name, name=self._disk_name, conf={})

        self._enabled = config.get("enabled", False)
        self._cd2_url = config.get("cd2_url")
        self._cd2_api_key = config.get("cd2_api_key")

        if not self._enabled:
            return

        if not self._cd2_url or not self._cd2_api_key:
            logger.error("【Cd2Disk】CloudDrive2 配置不完整，请检查地址和 API key")
            return

        try:
            self._cd2_api = Cd2Api(
                cd2_url=self._cd2_url,
                api_key=self._cd2_api_key,
                disk_name=self._disk_name,
            )
        except Exception as e:
            logger.error(f"【Cd2Disk】CloudDrive2 客户端创建失败: {e}")
            self._cd2_api = None

    def get_state(self) -> bool:
        return bool(self._enabled and self._cd2_api)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
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
                                "props": {"cols": 12, "md": 4},
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
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cd2_url",
                                            "label": "CloudDrive2 地址",
                                            "placeholder": "http://127.0.0.1:19798",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cd2_api_key",
                                            "label": "CloudDrive2 API key",
                                            "type": "password",
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
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                            "class": "mt-2",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "text": "说明：",
                                            },
                                            {
                                                "component": "div",
                                                "text": "• 仅支持已在 CloudDrive2 中挂载的网盘路径",
                                            },
                                            {
                                                "component": "div",
                                                "text": "• 鉴权方式为 Authorization: Bearer <API key>",
                                            },
                                            {
                                                "component": "div",
                                                "text": "• 插件已内置 clouddrive.proto 生成代码，无需安装 clouddrive 包",
                                            },
                                            {
                                                "component": "div",
                                                "text": "• 运行环境需具备 grpcio 与 protobuf 运行时",
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "cd2_url": "http://127.0.0.1:19798",
            "cd2_api_key": "",
        }

    def get_page(self) -> List[dict]:
        return []

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于接管系统存储模块实现
        """
        return {
            "list_files": self.list_files,
            "any_files": self.any_files,
            "download_file": self.download_file,
            "upload_file": self.upload_file,
            "delete_file": self.delete_file,
            "rename_file": self.rename_file,
            "get_file_item": self.get_file_item,
            "get_parent_item": self.get_parent_item,
            "snapshot_storage": self.snapshot_storage,
            "storage_usage": self.storage_usage,
            "support_transtype": self.support_transtype,
            "create_folder": self.create_folder,
            "exists": self.exists,
            "get_item": self.get_item,
        }

    @eventmanager.register(ChainEventType.StorageOperSelection)
    def storage_oper_selection(self, event: Event):
        """
        监听存储选择事件，返回当前类为操作对象
        """
        if not self.get_state():
            return

        event_data: StorageOperSelectionEventData = event.event_data
        if event_data.storage == self._disk_name:
            event_data.storage_oper = self._cd2_api  # noqa

    def list_files(self, fileitem: FileItem, recursion: bool = False) -> Optional[List[FileItem]]:
        """
        查询当前目录下所有目录和文件
        """
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return []

        api = self._cd2_api

        if recursion:
            result = api.iter_files(fileitem)
            if result is not None:
                return result

        def __get_files(_item: FileItem, _r: Optional[bool] = False):
            _items = api.list(_item)
            if _items:
                if _r:
                    for t in _items:
                        if t.type == "dir":
                            __get_files(t, _r)
                        else:
                            result_items.append(t)
                else:
                    result_items.extend(_items)

        result_items: List[FileItem] = []
        __get_files(fileitem, recursion)
        return result_items

    def any_files(self, fileitem: FileItem, extensions: Optional[List[str]] = None) -> Optional[bool]:
        """
        查询当前目录下是否存在指定扩展名任意文件
        """
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return False

        api = self._cd2_api

        def __any_file(_item: FileItem):
            _items = api.list(_item)
            if _items:
                if not extensions:
                    return True
                for t in _items:
                    if t.type == "file" and t.extension and f".{t.extension.lower()}" in extensions:
                        return True
                    if t.type == "dir" and __any_file(t):
                        return True
            return False

        return __any_file(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.create_folder(fileitem=fileitem, name=name)

    def download_file(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.download(fileitem, path)

    def upload_file(
        self,
        fileitem: FileItem,
        path: Path,
        new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.upload(fileitem, path, new_name)

    def delete_file(self, fileitem: FileItem) -> Optional[bool]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return False
        return self._cd2_api.delete(fileitem)

    def rename_file(self, fileitem: FileItem, name: str) -> Optional[bool]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return False
        return self._cd2_api.rename(fileitem, name)

    def exists(self, fileitem: FileItem) -> Optional[bool]:
        if fileitem.storage != self._disk_name:
            return None
        return True if self.get_item(fileitem) else False

    def get_item(self, fileitem: FileItem) -> Optional[FileItem]:
        if fileitem.storage != self._disk_name:
            return None
        return self.get_file_item(storage=fileitem.storage, path=Path(fileitem.path))

    def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]:
        if storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.get_item(path)

    def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]:
        if fileitem.storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.get_parent(fileitem)

    def snapshot_storage(
        self,
        storage: str,
        path: Path,
        last_snapshot_time: Optional[float] = None,
        max_depth: int = 5,
    ) -> Optional[Dict[str, Dict]]:
        """
        快照存储
        """
        if storage != self._disk_name:
            return None
        if not self._cd2_api:
            return {}

        api = self._cd2_api
        files_info: Dict[str, Dict] = {}

        def __snapshot_file(_fileitm: FileItem, current_depth: int = 0):
            try:
                if _fileitm.type == "dir":
                    if current_depth >= max_depth:
                        return

                    if (
                        getattr(self, "snapshot_check_folder_modtime", False)
                        and last_snapshot_time
                        and _fileitm.modify_time
                        and _fileitm.modify_time <= last_snapshot_time
                    ):
                        return

                    sub_files = api.list(_fileitm)
                    for sub_file in sub_files:
                        __snapshot_file(sub_file, current_depth + 1)
                else:
                    modify_time = getattr(_fileitm, "modify_time", 0) or 0
                    if not last_snapshot_time or modify_time > last_snapshot_time:
                        files_info[_fileitm.path] = {
                            "size": _fileitm.size or 0,
                            "modify_time": modify_time,
                            "type": _fileitm.type,
                        }
            except Exception as e:
                logger.debug(f"【Cd2Disk】Snapshot error for {_fileitm.path}: {e}")

        fileitem = api.get_item(path)
        if not fileitem:
            return {}

        __snapshot_file(fileitem)
        return files_info

    def storage_usage(self, storage: str) -> Optional[StorageUsage]:
        if storage != self._disk_name:
            return None
        if not self._cd2_api:
            return None
        return self._cd2_api.usage()

    def support_transtype(self, storage: str) -> Optional[dict]:
        if storage != self._disk_name:
            return None
        return {"move": "移动", "copy": "复制"}

    def stop_service(self):
        if self._cd2_api:
            self._cd2_api.close()
        self._cd2_api = None
