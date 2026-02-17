import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.directory import DirectoryHelper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo
from app.schemas.types import EventType
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class QbSmartQueue(_PluginBase):
    # 插件名称
    plugin_name = "qBittorrent 智能体积调度"
    # 插件描述
    plugin_desc = "基于下载体积动态管理 qBittorrent 队列，大文件排队、小文件插队，防止硬盘爆满"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.0.2"
    # 插件作者
    plugin_author = "baranwang"
    # 作者主页
    author_url = "https://github.com/baranwang"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "qbsmartqueue_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 2

    # 种子状态归类
    _ACTIVE_DL_STATES = {
        "downloading", "stalledDL", "metaDL",
        "checkingDL", "forcedDL", "allocating",
    }
    _PAUSED_DL_STATES = {
        "pausedDL", "stoppedDL", "queuedDL",
    }

    # 私有属性
    _event = threading.Event()
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "*/2 * * * *"
    _max_capacity_gb: float = 35
    _priority_mode: str = "age"
    _enable_smart_skip: bool = True
    _mponly: bool = True
    _download_paths: list = []
    _min_free_gb: float = 5
    _downloaders: list = []

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron") or "*/2 * * * *"
            self._max_capacity_gb = float(config.get("max_capacity_gb") or 35)
            self._priority_mode = config.get("priority_mode") or "age"
            self._enable_smart_skip = config.get("enable_smart_skip", True)
            self._mponly = config.get("mponly", True)
            self._download_paths = config.get("download_paths") or []
            self._min_free_gb = float(config.get("min_free_gb") or 5)
            self._downloaders = config.get("downloaders") or []

        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("qBittorrent 智能体积调度服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.manage_queue,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "cron": self._cron,
                "max_capacity_gb": self._max_capacity_gb,
                "priority_mode": self._priority_mode,
                "enable_smart_skip": self._enable_smart_skip,
                "mponly": self._mponly,
                "download_paths": self._download_paths,
                "min_free_gb": self._min_free_gb,
                "downloaders": self._downloaders,
            })
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return True if self._enabled and self._cron and self._downloaders else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/smart_queue",
                "event": EventType.PluginAction,
                "desc": "立即执行 qBittorrent 智能体积调度",
                "category": "qBittorrent",
                "data": {"action": "smart_queue"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self.get_state():
            return [
                {
                    "id": "QbSmartQueue",
                    "name": "qBittorrent 智能体积调度",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.manage_queue,
                    "kwargs": {},
                }
            ]
        return []

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        服务信息
        """
        if not self._downloaders:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"下载器 {service_name} 未连接，请检查配置")
            elif not DownloaderHelper().is_downloader(
                service_type="qbittorrent", service=service_info
            ):
                logger.warning(f"下载器 {service_name} 不是 qBittorrent 类型，跳过")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的 qBittorrent 下载器，请检查配置")
            return None

        return active_services

    @eventmanager.register(EventType.PluginAction)
    def handle_smart_queue_command(self, event: Event):
        """
        处理远程命令
        """
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "smart_queue":
                return
        logger.info("收到远程命令，立即执行 qBittorrent 智能体积调度")
        self.manage_queue()

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event):
        """
        新下载添加后立即触发队列管理
        """
        if not self._enabled:
            return
        logger.info("检测到新下载任务，触发 qBittorrent 智能体积调度")
        self.manage_queue()

    def manage_queue(self):
        """
        核心调度逻辑：遍历所有已配置的 qBittorrent 下载器，分别执行队列管理
        """
        with lock:
            services = self.service_infos
            if not services:
                return

            for service_name, service_info in services.items():
                try:
                    self._manage_single_downloader(service_name, service_info)
                except Exception as e:
                    logger.error(f"处理下载器 {service_name} 时出错: {e}")

    def _manage_single_downloader(
        self, service_name: str, service_info: ServiceInfo
    ):
        """
        对单个下载器执行队列管理
        """
        downloader = service_info.instance
        max_capacity_bytes = self._max_capacity_gb * (1024 ** 3)

        # ── 1. 获取所有种子 ──
        if self._mponly:
            torrents, error = downloader.get_torrents(tags=settings.TORRENT_TAG)
        else:
            torrents, error = downloader.get_torrents()

        if error:
            logger.error(f"[{service_name}] 获取种子列表失败: {error}")
            return

        if not torrents:
            logger.debug(f"[{service_name}] 没有种子")
            return

        # ── 2. 磁盘空间检查（按种子 save_path 匹配监控目录，精准暂停） ──
        low_space_paths: list = []
        if self._download_paths:
            min_free_bytes = self._min_free_gb * (1024 ** 3)
            for dp in self._download_paths:
                free_bytes = SystemUtils.free_space(Path(dp))
                if free_bytes < min_free_bytes:
                    low_space_paths.append(dp)
                    logger.warning(
                        f"[{service_name}] 目录 {dp} 所在磁盘剩余空间 "
                        f"{StringUtils.str_filesize(free_bytes)} "
                        f"低于阈值 {self._min_free_gb} GB"
                    )
            if low_space_paths:
                # 只暂停 save_path 属于低空间目录的活跃种子
                paused_by_disk = []
                for t in torrents:
                    if t.get("state") not in self._ACTIVE_DL_STATES:
                        continue
                    t_save = t.get("save_path", "")
                    if t_save and any(
                        t_save == lp or t_save.startswith(lp.rstrip("/") + "/")
                        for lp in low_space_paths
                    ):
                        downloader.stop_torrents(ids=[t.get("hash")])
                        paused_by_disk.append(t.get("name", ""))
                if paused_by_disk:
                    logger.info(
                        f"[{service_name}] 磁盘空间不足，暂停 {len(paused_by_disk)} 个种子"
                    )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【qBittorrent 智能体积调度】",
                            text=(
                                f"下载器: {service_name}\n"
                                f"磁盘空间不足目录: {', '.join(low_space_paths)}\n"
                                f"已暂停 {len(paused_by_disk)} 个对应种子:\n"
                                + ", ".join(paused_by_disk[:5])
                            ),
                        )
                    # 重新获取种子列表（状态已变化）
                    if self._mponly:
                        torrents, _ = downloader.get_torrents(tags=settings.TORRENT_TAG)
                    else:
                        torrents, _ = downloader.get_torrents()
                    if not torrents:
                        return

        # ── 3. 分类种子 ──
        active_torrents = []
        paused_torrents = []
        for t in torrents:
            state = t.get("state")
            if state in self._ACTIVE_DL_STATES:
                active_torrents.append(t)
            elif state in self._PAUSED_DL_STATES:
                paused_torrents.append(t)

        active_left = sum(t.get("amount_left", 0) for t in active_torrents)

        logger.info(
            f"[{service_name}] 活跃下载: {len(active_torrents)} 个, "
            f"剩余体积: {StringUtils.str_filesize(active_left)}, "
            f"容量上限: {self._max_capacity_gb} GB, "
            f"待调度: {len(paused_torrents)} 个"
        )

        # ── 4. 溢出保护：活跃下载超限则暂停最新的任务 ──
        paused_by_overflow = []
        if active_left > max_capacity_bytes:
            overflow_candidates = sorted(
                active_torrents, key=lambda x: x.get("added_on", 0), reverse=True
            )
            for t in overflow_candidates:
                if active_left <= max_capacity_bytes:
                    break
                t_hash = t.get("hash")
                t_left = t.get("amount_left", 0)
                t_name = t.get("name", "")
                downloader.stop_torrents(ids=[t_hash])
                active_left -= t_left
                paused_by_overflow.append(t_name)
                logger.info(
                    f"[{service_name}] 溢出保护：暂停 {t_name} "
                    f"(剩余 {StringUtils.str_filesize(t_left)})"
                )
            # 溢出暂停的种子加入待调度队列
            if paused_by_overflow:
                # 重新获取种子状态（暂停后状态会变化）
                if self._mponly:
                    torrents, _ = downloader.get_torrents(tags=settings.TORRENT_TAG)
                else:
                    torrents, _ = downloader.get_torrents()
                paused_torrents = [
                    t for t in (torrents or [])
                    if t.get("state") in self._PAUSED_DL_STATES
                ]
                active_torrents = [
                    t for t in (torrents or [])
                    if t.get("state") in self._ACTIVE_DL_STATES
                ]
                active_left = sum(t.get("amount_left", 0) for t in active_torrents)

        # ── 5. 排序等待队列 ──
        if self._priority_mode == "size":
            paused_torrents.sort(key=lambda x: x.get("total_size", 0))
        else:
            paused_torrents.sort(key=lambda x: x.get("added_on", 0))

        # ── 6. 放行逻辑 ──
        released = []
        skipped = []
        for t in paused_torrents:
            t_left = t.get("amount_left", 0)
            t_name = t.get("name", "")
            t_hash = t.get("hash")
            t_size = t.get("total_size", 0)
            t_save = t.get("save_path", "")

            # 跳过 save_path 处于低空间磁盘的种子，不放行
            if low_space_paths and t_save and any(
                t_save == lp or t_save.startswith(lp.rstrip("/") + "/")
                for lp in low_space_paths
            ):
                logger.debug(
                    f"[{service_name}] 磁盘空间不足，跳过放行: {t_name} "
                    f"(目录 {t_save})"
                )
                continue

            if t_left == 0:
                # 已下载完成但处于暂停状态，直接恢复
                downloader.start_torrents(ids=[t_hash])
                released.append(t_name)
                logger.info(f"[{service_name}] 恢复已完成种子: {t_name}")
                continue

            if active_left + t_left <= max_capacity_bytes:
                downloader.start_torrents(ids=[t_hash])
                active_left += t_left
                released.append(t_name)
                logger.info(
                    f"[{service_name}] 放行: {t_name} "
                    f"(体积 {StringUtils.str_filesize(t_size)}, "
                    f"剩余 {StringUtils.str_filesize(t_left)})"
                )
            else:
                if self._enable_smart_skip:
                    skipped.append(t_name)
                    logger.debug(
                        f"[{service_name}] 跳过大文件: {t_name} "
                        f"(剩余 {StringUtils.str_filesize(t_left)})"
                    )
                else:
                    break

        # ── 7. 防死锁：无活跃下载且有等待任务时，强制放行第一个（排除低空间目录） ──
        if (
            not active_torrents
            and not released
            and paused_torrents
        ):
            for candidate in paused_torrents:
                c_save = candidate.get("save_path", "")
                # 跳过低空间目录的种子
                if low_space_paths and c_save and any(
                    c_save == lp or c_save.startswith(lp.rstrip("/") + "/")
                    for lp in low_space_paths
                ):
                    continue
                c_hash = candidate.get("hash")
                c_name = candidate.get("name", "")
                downloader.start_torrents(ids=[c_hash])
                released.append(c_name)
                logger.info(
                    f"[{service_name}] 防死锁：强制放行 {c_name}"
                )
                break

        # ── 8. 通知 ──
        if self._notify and (released or paused_by_overflow):
            text_parts = [f"下载器: {service_name}"]
            if paused_by_overflow:
                text_parts.append(
                    f"溢出保护暂停 {len(paused_by_overflow)} 个: "
                    + ", ".join(paused_by_overflow[:5])
                )
            if released:
                text_parts.append(
                    f"放行 {len(released)} 个: "
                    + ", ".join(released[:5])
                )
            if skipped:
                text_parts.append(f"跳过大文件 {len(skipped)} 个")
            text_parts.append(
                f"当前活跃下载剩余: {StringUtils.str_filesize(active_left)} / "
                f"{self._max_capacity_gb} GB"
            )
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【qBittorrent 智能体积调度】",
                text="\n".join(text_parts),
            )

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    # ── 开关行 ──
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
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 下载器选择 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "downloaders",
                                            "label": "下载器",
                                            "items": [
                                                {
                                                    "title": config.name,
                                                    "value": config.name,
                                                }
                                                for config in DownloaderHelper()
                                                .get_configs()
                                                .values()
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # ── 执行周期 + 容量上限 ──
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
                                            "placeholder": "*/2 * * * *",
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
                                            "model": "max_capacity_gb",
                                            "label": "最大并发下载体积 (GB)",
                                            "placeholder": "35",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 排队策略 + 仅 MP 任务 ──
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "priority_mode",
                                            "label": "排队策略",
                                            "items": [
                                                {
                                                    "title": "先来先到",
                                                    "value": "age",
                                                },
                                                {
                                                    "title": "小文件优先",
                                                    "value": "size",
                                                },
                                            ],
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
                                            "model": "enable_smart_skip",
                                            "label": "小文件插队",
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
                                            "model": "mponly",
                                            "label": "仅 MoviePilot 任务",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 磁盘保护 ──
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
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "download_paths",
                                            "label": "监控下载目录 (磁盘空间检测)",
                                            "items": [
                                                {
                                                    "title": d.download_path,
                                                    "value": d.download_path,
                                                }
                                                for d in DirectoryHelper().get_local_download_dirs()
                                                if d.download_path
                                            ],
                                            "hint": "不选则不检测磁盘空间",
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
                                            "model": "min_free_gb",
                                            "label": "最低磁盘剩余空间 (GB)",
                                            "placeholder": "5",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 说明 ──
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
                                            "text": (
                                                "根据正在下载任务的剩余体积总和动态管理 qBittorrent 队列：\n\n"
                                                "1. 溢出保护：活跃下载超限时暂停最新任务\n\n"
                                                "2. 智能放行：按策略排序，逐个放行不超限的任务\n\n"
                                                "3. 小文件插队：大文件塞不下时跳过，放行后面小文件\n\n"
                                                "4. 防死锁：无活跃下载时强制放行第一个\n\n"
                                                "5. 磁盘保护：剩余空间低于阈值时暂停所有下载"
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
            "onlyonce": False,
            "cron": "*/2 * * * *",
            "max_capacity_gb": 35,
            "priority_mode": "age",
            "enable_smart_skip": True,
            "mponly": True,
            "download_paths": [],
            "min_free_gb": 5,
            "downloaders": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"qBittorrent 智能体积调度停止服务异常: {e}")
