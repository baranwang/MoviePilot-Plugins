import base64
import datetime
import os
import random
import re
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils

from app.plugins.mediacovergenerator.cover_style import create_cover


class MediaCoverGenerator(_PluginBase):
    """媒体库封面生成插件"""

    # 插件元数据
    plugin_name = "媒体库封面生成"
    plugin_desc = "自动为 Emby / Jellyfin 媒体库生成多图旋转海报封面"
    plugin_icon = "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/icons/emby.png"
    plugin_version = "1.0.2"
    plugin_author = "baranwang"
    author_url = "https://github.com/baranwang/MoviePilot-Plugins"
    plugin_config_prefix = "mediacovergenerator_"
    plugin_order = 2
    auth_level = 1

    # 线程控制
    _event = threading.Event()

    # 私有属性
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _cron = None
    _selected_servers = []
    _exclude_libraries = []
    _title_config = ""
    _covers_input = ""
    _covers_output = ""
    _covers_path: Path = None
    _servers = None
    _all_libraries = []

    def init_plugin(self, config: dict = None):
        self.mediaserver_helper = MediaServerHelper()
        data_path = self.get_data_path()
        (data_path / "fonts").mkdir(parents=True, exist_ok=True)
        (data_path / "covers").mkdir(parents=True, exist_ok=True)
        self._covers_path = data_path / "covers"

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron")
            self._selected_servers = config.get("selected_servers") or []
            self._exclude_libraries = config.get("exclude_libraries") or []
            self._title_config = config.get("title_config") or ""
            self._covers_input = config.get("covers_input") or ""
            self._covers_output = config.get("covers_output") or ""

        if self._selected_servers:
            self._servers = self.mediaserver_helper.get_services(
                name_filters=self._selected_servers
            )
            self._all_libraries = []
            for server, service in self._servers.items():
                if not service.instance.is_inactive():
                    self._all_libraries.extend(self._get_all_libraries(server, service))
                else:
                    logger.info(f"媒体服务器 {server} 未连接")
        else:
            logger.info("未选择媒体服务器")

        # 停止现有任务
        self.stop_service()

        # 立即运行一次
        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self._update_all_libraries,
                trigger="date",
                run_date=datetime.datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + datetime.timedelta(seconds=3),
            )
            logger.info("媒体库封面更新服务启动，立即运行一次")
            self._onlyonce = False
            self._update_config()
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def _update_config(self):
        """保存配置"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "selected_servers": self._selected_servers,
                "exclude_libraries": self._exclude_libraries,
                "title_config": self._title_config,
                "covers_input": self._covers_input,
                "covers_output": self._covers_output,
            }
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "MediaCoverGenerator",
                    "name": "媒体库封面更新服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self._update_all_libraries,
                    "kwargs": {},
                }
            ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    # 基本设置
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 定时任务
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
                                            "label": "执行周期",
                                            "placeholder": "0 0 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 媒体服务器选择
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "selected_servers",
                                            "label": "媒体服务器",
                                            "multiple": True,
                                            "chips": True,
                                            "items": [
                                                {"title": s.name, "value": s.name}
                                                for s in self.mediaserver_helper.get_services().values()
                                            ]
                                            if self.mediaserver_helper
                                            else [],
                                            "hint": "选择需要更新封面的媒体服务器",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "exclude_libraries",
                                            "label": "排除媒体库",
                                            "multiple": True,
                                            "chips": True,
                                            "items": self._all_libraries or [],
                                            "hint": "选择需要排除的媒体库",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 自定义路径
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "covers_input",
                                            "label": "自定义图片目录（可选）",
                                            "prependInnerIcon": "mdi-file-image",
                                            "hint": "海报图片存放在以媒体库名命名的子目录下，如 /path/华语电影/1.jpg",
                                            "persistentHint": True,
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
                                            "model": "covers_output",
                                            "label": "封面另存目录（可选）",
                                            "prependInnerIcon": "mdi-file-image",
                                            "hint": "生成的封面在此目录另存一份",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 标题配置
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
                                            "text": "未配置的媒体库将默认使用媒体库名称作为封面中文标题，无英文副标题",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAceEditor",
                                        "props": {
                                            "modelvalue": "title_config",
                                            "lang": "yaml",
                                            "theme": "monokai",
                                            "style": "height: 20rem",
                                            "label": "中英标题配置",
                                            "placeholder": "媒体库名称:\n- 中文标题\n- 英文标题",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "selected_servers": [],
            "exclude_libraries": [],
            "title_config": "",
            "covers_input": "",
            "covers_output": "",
        }

    def get_page(self) -> List[dict]:
        pass

    # ============================================================
    # 核心逻辑
    # ============================================================

    def _update_all_libraries(self):
        """更新所有媒体库的封面"""
        if not self._servers:
            logger.warning("未配置媒体服务器")
            return

        logger.info("开始更新所有媒体库封面...")

        for server, service in self._servers.items():
            logger.info(f"当前服务器: {server}")
            libraries = self._get_server_libraries(service)
            if not libraries:
                logger.warning(f"服务器 {server} 的媒体库列表获取失败")
                continue

            for library in libraries:
                if self._event.is_set():
                    logger.info("媒体库封面更新服务停止")
                    return

                if service.type == "emby":
                    library_id = library.get("Id")
                else:
                    library_id = library.get("ItemId")

                if f"{server}-{library_id}" in self._exclude_libraries:
                    logger.info(f"媒体库 {server}：{library['Name']} 已忽略")
                    continue

                if self._update_library(service, library):
                    logger.info(f"媒体库 {server}：{library['Name']} 封面更新成功")
                else:
                    logger.warning(f"媒体库 {server}：{library['Name']} 封面更新失败")

        logger.info("所有媒体库封面更新完成")

    def _update_library(self, service, library):
        """更新单个媒体库的封面"""
        library_name = library["Name"]
        logger.info(f"媒体库 {service.name}：{library_name} 开始准备更新封面")

        title = self._get_library_title(library_name)

        # 优先使用自定义图片
        custom_images = self._check_custom_images(library_name)
        if custom_images:
            logger.info(f"媒体库 {service.name}：{library_name} 从自定义路径获取封面")
            library_dir = Path(self._covers_input) / library_name
        else:
            # 从服务器获取海报
            items = self._get_library_items(service, library)
            if not items:
                logger.warning(f"媒体库 {service.name}：{library_name} 无可用的媒体项")
                return False

            # 下载海报到本地
            library_dir = self._covers_path / library_name
            self._download_posters(service, library_name, items[:9])

        # 补全 1-9.jpg
        if not self._prepare_library_images(library_dir):
            return False

        # 生成封面
        font_path = self._get_font_paths()
        image_data = create_cover(str(library_dir), title, font_path)

        if not image_data:
            return False

        # 另存一份
        if self._covers_output:
            self._save_image_locally(image_data, f"{library_name}.jpg")

        # 上传到媒体服务器
        return self._set_library_image(service, library, image_data)

    # ============================================================
    # 字体管理
    # ============================================================

    def _get_font_paths(self) -> Tuple[str, str]:
        """
        获取字体文件路径

        1. 搜索本地候选目录并验证可用性
        2. 找不到或不可用则从 GitHub 下载到数据目录
        """
        zh_name = "NotoSansSC-Bold.ttf"
        en_name = "Lexend-SemiBold.ttf"

        # 数据目录下的字体目录
        data_font_dir = self.get_data_path() / "fonts"
        data_font_dir.mkdir(parents=True, exist_ok=True)

        # 候选目录（按优先级）
        candidate_dirs = [
            Path(__file__).parent / "fonts",
            data_font_dir,
        ]
        try:
            import app.plugins.mediacovergenerator.cover_style as _cs
            candidate_dirs.insert(1, Path(_cs.__file__).parent / "fonts")
        except Exception:
            pass

        zh_font = self._find_valid_font(zh_name, candidate_dirs)
        en_font = self._find_valid_font(en_name, candidate_dirs)

        # 找不到或不可用则下载
        if not zh_font:
            zh_font = self._download_font(zh_name, data_font_dir)
        if not en_font:
            en_font = self._download_font(en_name, data_font_dir)

        logger.info(f"字体路径 - 中文: {zh_font}, 英文: {en_font}")
        return (zh_font, en_font)

    @staticmethod
    def _find_valid_font(name: str, dirs: list) -> Optional[str]:
        """在候选目录中搜索字体文件并验证能被 PIL 加载"""
        from PIL import ImageFont
        for d in dirs:
            path = d / name
            if path.is_file():
                try:
                    ImageFont.truetype(str(path), 12)
                    logger.info(f"找到有效字体: {path}")
                    return str(path)
                except Exception as e:
                    logger.warning(f"字体文件无效（可能是 Git LFS 指针）: {path}, 错误: {e}")
        return None

    def _download_font(self, name: str, target_dir: Path) -> str:
        """从 GitHub 下载字体文件"""
        target = target_dir / name
        base_urls = [
            f"https://github.com/baranwang/MoviePilot-Plugins/raw/main/plugins.v2/mediacovergenerator/fonts/{name}",
            f"https://github.com/justzerock/MoviePilot-Plugins/raw/main/plugins.v2/mediacovergenerator/fonts/{name}",
        ]

        # 尝试通过 GitHub 代理加速
        proxy_hosts = [
            None,  # 直连
            "https://mirror.ghproxy.com/",
            "https://ghproxy.cc/",
        ]

        from PIL import ImageFont
        for base_url in base_urls:
            for proxy in proxy_hosts:
                url = f"{proxy}{base_url}" if proxy else base_url
                try:
                    logger.info(f"正在下载字体 {name}: {url}")
                    response = RequestUtils(timeout=30).get_res(url)
                    if response and response.status_code == 200 and len(response.content) > 1000:
                        target.write_bytes(response.content)
                        # 验证下载的文件是否有效
                        try:
                            ImageFont.truetype(str(target), 12)
                            logger.info(f"字体下载成功: {name} -> {target} ({len(response.content)} bytes)")
                            return str(target)
                        except Exception:
                            logger.warning(f"下载的字体文件无效，跳过: {url}")
                            target.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"字体下载失败 {url}: {e}")
                    continue

        logger.error(f"字体 {name} 下载失败，所有源均不可用")
        return str(target)  # 返回预期路径，让后续逻辑走 fallback

    # ============================================================
    # 标题配置
    # ============================================================

    def _get_library_title(self, library_name: str) -> Tuple[str, str]:
        """从 YAML 配置获取媒体库的中英文标题"""
        zh_title = library_name
        en_title = ""

        if not self._title_config:
            return (zh_title, en_title)

        try:
            # 预处理 YAML
            yaml_str = self._title_config.replace("：", ":").replace("\t", "  ")
            data = yaml.safe_load(yaml_str)
            if isinstance(data, dict):
                for lib_name, titles in data.items():
                    if lib_name == library_name and isinstance(titles, list) and len(titles) >= 2:
                        zh_title = titles[0]
                        en_title = titles[1]
                        break
        except Exception as e:
            logger.warning(f"标题配置解析失败，将使用库名: {library_name}，错误: {e}")

        return (zh_title, en_title)

    # ============================================================
    # 自定义图片
    # ============================================================

    def _check_custom_images(self, library_name: str) -> Optional[List[str]]:
        """检查是否有自定义图片"""
        if not self._covers_input:
            return None

        library_dir = os.path.join(self._covers_input, library_name)
        if not os.path.isdir(library_dir):
            return None

        images = sorted(
            [
                os.path.join(library_dir, f)
                for f in os.listdir(library_dir)
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp"))
            ]
        )
        return images if images else None

    def _prepare_library_images(self, library_dir) -> bool:
        """
        确保目录中有 1-9.jpg
        缺失的号码从已有图片中随机复制补全
        """
        library_dir = str(library_dir)
        os.makedirs(library_dir, exist_ok=True)

        existing_numbers = []
        missing_numbers = []
        for i in range(1, 10):
            target = os.path.join(library_dir, f"{i}.jpg")
            if os.path.exists(target):
                existing_numbers.append(i)
            else:
                missing_numbers.append(i)

        if not missing_numbers:
            return True

        # 找到可用的源图片
        source_files = []
        for f in os.listdir(library_dir):
            if not re.match(r"^[1-9]\.jpg$", f, re.IGNORECASE):
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    source_files.append(os.path.join(library_dir, f))

        if not source_files:
            if existing_numbers:
                source_files = [os.path.join(library_dir, f"{i}.jpg") for i in existing_numbers]
            else:
                logger.warning(f"目录 {library_dir} 中没有可用图片")
                return False

        # 补全缺失的文件
        last_used = None
        for num in missing_numbers:
            target = os.path.join(library_dir, f"{num}.jpg")
            available = [s for s in source_files if s != last_used] or source_files
            selected = random.choice(available)
            last_used = selected

            try:
                shutil.copy(selected, target)
            except Exception as e:
                logger.error(f"复制文件失败: {e}")
                return False

        return True

    # ============================================================
    # 媒体服务器交互
    # ============================================================

    def _get_server_libraries(self, service) -> list:
        """获取媒体服务器的媒体库列表"""
        try:
            if service.type == "emby":
                url = "[HOST]emby/Library/VirtualFolders/Query?api_key=[APIKEY]"
            else:
                url = "[HOST]emby/Library/VirtualFolders/?api_key=[APIKEY]"

            res = service.instance.get_data(url=url)
            if res:
                data = res.json()
                if service.type == "emby":
                    return data.get("Items", [])
                return data
        except Exception as e:
            logger.error(f"获取媒体库列表失败：{e}")
        return []

    def _get_all_libraries(self, server, service) -> list:
        """获取所有媒体库用于排除列表"""
        lib_items = []
        try:
            libraries = self._get_server_libraries(service)
            for library in libraries:
                library_id = (
                    library.get("Id")
                    if service.type == "emby"
                    else library.get("ItemId")
                )
                if library["Name"] and library_id:
                    lib_items.append(
                        {
                            "title": f"{server}: {library['Name']}",
                            "value": f"{server}-{library_id}",
                        }
                    )
        except Exception as e:
            logger.error(f"获取所有媒体库失败：{e}")
        return lib_items

    def _get_library_items(self, service, library, limit: int = 20) -> list:
        """获取媒体库中的媒体项"""
        try:
            library_id = (
                library.get("Id")
                if service.type == "emby"
                else library.get("ItemId")
            )

            url = (
                f"[HOST]emby/Items/?api_key=[APIKEY]"
                f"&ParentId={library_id}&SortBy=Random&Limit={limit}"
                f"&IncludeItemTypes=Movie,Series&Recursive=True"
                f"&SortOrder=Descending"
            )

            res = service.instance.get_data(url=url)
            if res:
                items = res.json().get("Items", [])
                # 筛选有图片的项目
                return [
                    item
                    for item in items
                    if (item.get("ImageTags") and item["ImageTags"].get("Primary"))
                    or item.get("BackdropImageTags")
                    or item.get("ParentBackdropImageTags")
                ]
        except Exception as e:
            logger.error(f"获取媒体项失败：{e}")
        return []

    def _download_posters(self, service, library_name: str, items: list):
        """下载海报到本地"""
        subdir = self._covers_path / library_name
        subdir.mkdir(parents=True, exist_ok=True)

        for i, item in enumerate(items, 1):
            filepath = subdir / f"{i}.jpg"

            # 获取海报图片 URL（优先使用竖版海报 Primary）
            image_url = None
            if item.get("ImageTags") and item["ImageTags"].get("Primary"):
                item_id = item["Id"]
                tag = item["ImageTags"]["Primary"]
                image_url = f"[HOST]emby/Items/{item_id}/Images/Primary?tag={tag}&api_key=[APIKEY]"
            elif item.get("BackdropImageTags") and len(item["BackdropImageTags"]) > 0:
                item_id = item["Id"]
                tag = item["BackdropImageTags"][0]
                image_url = f"[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]"
            elif item.get("ParentBackdropImageTags") and len(item["ParentBackdropImageTags"]) > 0:
                item_id = item.get("ParentBackdropItemId")
                tag = item["ParentBackdropImageTags"][0]
                image_url = f"[HOST]emby/Items/{item_id}/Images/Backdrop/0?tag={tag}&api_key=[APIKEY]"

            if not image_url:
                continue

            # 下载图片
            try:
                res = service.instance.get_data(url=image_url)
                if res and res.status_code == 200:
                    with open(filepath, "wb") as f:
                        f.write(res.content)
            except Exception as e:
                logger.warning(f"下载海报失败: {e}")

    def _set_library_image(self, service, library, image_base64: str) -> bool:
        """设置媒体库封面"""
        try:
            library_id = (
                library.get("Id")
                if service.type == "emby"
                else library.get("ItemId")
            )
            url = f"[HOST]emby/Items/{library_id}/Images/Primary?api_key=[APIKEY]"

            res = service.instance.post_data(
                url=url,
                data=image_base64,
                headers={"Content-Type": "image/png"},
            )

            if res and res.status_code in [200, 204]:
                return True
            else:
                code = res.status_code if res else "No response"
                logger.error(f"设置「{library['Name']}」封面失败，错误码：{code}")
                return False
        except Exception as e:
            logger.error(f"设置「{library['Name']}」封面失败：{e}")
        return False

    def _save_image_locally(self, image_base64: str, filename: str):
        """另存封面图片到本地"""
        try:
            if not self._covers_output:
                return
            os.makedirs(self._covers_output, exist_ok=True)
            filepath = os.path.join(self._covers_output, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(image_base64))
            logger.info(f"封面已另存到: {filepath}")
        except Exception as e:
            logger.error(f"保存封面到本地失败: {e}")

    def stop_service(self):
        """停止服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止服务失败: {e}")
