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
    plugin_name = "qBittorrent 队列调度"
    # 插件描述
    plugin_desc = "按下载体积动态调度 qBittorrent 队列，自动排队放行，防止磁盘爆满"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.1.5"
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
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = "*/2 * * * *"
    _max_capacity_gb: float = 35
    _priority_mode: str = "age"
    _enable_smart_skip: bool = True
    _enable_low_speed_tolerance: bool = True
    _low_speed_threshold_kib: int = 100
    _low_speed_stalled_only: bool = False
    _mponly: bool = True
    _min_free_gb: float = 5

    def init_plugin(self, config: dict = None):
        self._event = threading.Event()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._download_paths: list = []
        self._downloaders: list = []
        self._downloader_helper = DownloaderHelper()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron") or "*/2 * * * *"
            self._max_capacity_gb = float(config.get("max_capacity_gb") or 35)
            self._priority_mode = config.get("priority_mode") or "age"
            self._enable_smart_skip = config.get("enable_smart_skip", True)
            self._enable_low_speed_tolerance = config.get(
                "enable_low_speed_tolerance", True
            )
            self._low_speed_threshold_kib = int(
                config.get("low_speed_threshold_kib") or 100
            )
            self._low_speed_stalled_only = config.get(
                "low_speed_stalled_only", False
            )
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
                "enable_low_speed_tolerance": self._enable_low_speed_tolerance,
                "low_speed_threshold_kib": self._low_speed_threshold_kib,
                "low_speed_stalled_only": self._low_speed_stalled_only,
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

        services = self._downloader_helper.get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"下载器 {service_name} 未连接，请检查配置")
            elif not self._downloader_helper.is_downloader(
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
        torrents, error = self._fetch_torrents(downloader)

        if error:
            logger.error(f"[{service_name}] 获取种子列表失败: {error}")
            return

        if not torrents:
            logger.debug(f"[{service_name}] 没有种子")
            return

        free_space_map = self._get_free_space_map()

        # ── 2. 磁盘空间检查（按种子 save_path 匹配监控目录，精准暂停） ──
        low_space_paths: list = []
        if free_space_map:
            min_free_bytes = self._min_free_gb * (1024 ** 3)
            low_space_paths = [
                dp for dp, free_bytes in free_space_map.items()
                if free_bytes < min_free_bytes
            ]
            for dp in low_space_paths:
                logger.warning(
                    f"[{service_name}] 目录 {dp} 所在磁盘剩余空间 "
                    f"{StringUtils.str_filesize(free_space_map.get(dp, 0))} "
                    f"低于阈值 {self._min_free_gb} GB"
                )

        low_space_match_cache: Dict[str, bool] = {}
        if low_space_paths:
            # 只暂停 save_path 属于低空间目录的活跃种子
            paused_by_disk = []
            paused_by_disk_ids = []
            for t in torrents:
                if t.get("state") not in self._ACTIVE_DL_STATES:
                    continue
                t_save = t.get("save_path", "")
                if not t_save:
                    continue

                is_low_space_path = low_space_match_cache.get(t_save)
                if is_low_space_path is None:
                    is_low_space_path = self._is_path_under_paths(
                        save_path=t_save, paths=low_space_paths
                    )
                    low_space_match_cache[t_save] = is_low_space_path

                if is_low_space_path:
                    t_hash = t.get("hash")
                    if not t_hash:
                        continue
                    paused_by_disk_ids.append(t_hash)
                    paused_by_disk.append(t.get("name", ""))

            self._stop_torrent_ids(downloader, paused_by_disk_ids)
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
                torrents, _ = self._fetch_torrents(downloader)
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

        # ── 3.1 使用已采样的磁盘虚拟剩余空间 map ──

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

            if self._enable_low_speed_tolerance and self._low_speed_threshold_kib > 0:
                normal_candidates = []
                low_speed_candidates = []
                for torrent in overflow_candidates:
                    if self._is_low_speed_torrent(torrent):
                        low_speed_candidates.append(torrent)
                    else:
                        normal_candidates.append(torrent)

                if low_speed_candidates:
                    tolerance_scope = "stalledDL" if self._low_speed_stalled_only else "全部活跃状态"
                    logger.info(
                        f"[{service_name}] 低速宽容生效：优先保留 {len(low_speed_candidates)} 个低速种子 "
                        f"(阈值 {self._low_speed_threshold_kib} KiB/s, 范围 {tolerance_scope})"
                    )
                overflow_candidates = normal_candidates + low_speed_candidates

            overflow_stop_ids = []
            paused_low_speed_count = 0
            for t in overflow_candidates:
                if active_left <= max_capacity_bytes:
                    break
                t_hash = t.get("hash")
                if not t_hash:
                    continue
                t_left = t.get("amount_left", 0)
                t_name = t.get("name", "")
                overflow_stop_ids.append(t_hash)
                active_left -= t_left
                paused_by_overflow.append(t_name)
                if self._is_low_speed_torrent(t):
                    paused_low_speed_count += 1
                logger.info(
                    f"[{service_name}] 溢出保护：暂停 {t_name} "
                    f"(剩余 {StringUtils.str_filesize(t_left)})"
                )

            if paused_low_speed_count:
                logger.warning(
                    f"[{service_name}] 低速宽容回退：仍暂停 {paused_low_speed_count} 个低速种子以满足容量上限"
                )

            self._stop_torrent_ids(downloader, overflow_stop_ids)
            # 溢出暂停的种子加入待调度队列
            if paused_by_overflow:
                # 重新获取种子状态（暂停后状态会变化）
                torrents, _ = self._fetch_torrents(downloader)
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
        skipped_disk = []
        release_ids = []
        matched_path_cache: Dict[str, Optional[str]] = {}
        for t in paused_torrents:
            t_left = t.get("amount_left", 0)
            t_name = t.get("name", "")
            t_hash = t.get("hash")
            t_size = t.get("total_size", 0)
            t_save = t.get("save_path", "")

            # 跳过 save_path 处于低空间磁盘的种子，不放行
            if low_space_paths and t_save:
                is_low_space_path = low_space_match_cache.get(t_save)
                if is_low_space_path is None:
                    is_low_space_path = self._is_path_under_paths(
                        save_path=t_save, paths=low_space_paths
                    )
                    low_space_match_cache[t_save] = is_low_space_path

                if is_low_space_path:
                    logger.debug(
                        f"[{service_name}] 磁盘空间不足，跳过放行: {t_name} "
                        f"(目录 {t_save})"
                    )
                    continue

            if t_left == 0:
                # 已下载完成但处于暂停状态，直接恢复
                if t_hash:
                    release_ids.append(t_hash)
                    released.append(t_name)
                    logger.info(f"[{service_name}] 恢复已完成种子: {t_name}")
                continue

            matched_path = matched_path_cache.get(t_save)
            if matched_path is None and t_save not in matched_path_cache:
                matched_path = self._match_download_path(t_save)
                matched_path_cache[t_save] = matched_path

            # 磁盘空间预检：计算放行后磁盘是否还能容纳
            if not self._check_disk_budget(
                t_save, t_left, free_space_map, matched_path
            ):
                skipped_disk.append(t_name)
                logger.info(
                    f"[{service_name}] 磁盘空间不足以容纳，跳过: {t_name} "
                    f"(需要 {StringUtils.str_filesize(t_left)}, "
                    f"目录 {t_save})"
                )
                continue

            if active_left + t_left <= max_capacity_bytes:
                if t_hash:
                    release_ids.append(t_hash)
                    active_left += t_left
                    # 扣减虚拟磁盘空间
                    self._deduct_disk_budget(
                        t_save, t_left, free_space_map, matched_path
                    )
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
                c_left = candidate.get("amount_left", 0)
                # 跳过低空间目录的种子
                if low_space_paths and c_save:
                    is_low_space_path = low_space_match_cache.get(c_save)
                    if is_low_space_path is None:
                        is_low_space_path = self._is_path_under_paths(
                            save_path=c_save, paths=low_space_paths
                        )
                        low_space_match_cache[c_save] = is_low_space_path
                    if is_low_space_path:
                        continue

                matched_path = matched_path_cache.get(c_save)
                if matched_path is None and c_save not in matched_path_cache:
                    matched_path = self._match_download_path(c_save)
                    matched_path_cache[c_save] = matched_path

                # 磁盘空间安全检查：即使防死锁也不能让磁盘写满
                if not self._check_disk_budget(
                    c_save, c_left, free_space_map, matched_path
                ):
                    logger.warning(
                        f"[{service_name}] 防死锁：磁盘空间不足，跳过 {candidate.get('name', '')} "
                        f"(需要 {StringUtils.str_filesize(c_left)}, 目录 {c_save})"
                    )
                    continue
                c_hash = candidate.get("hash")
                if not c_hash:
                    continue
                c_name = candidate.get("name", "")
                release_ids.append(c_hash)
                self._deduct_disk_budget(
                    c_save, c_left, free_space_map, matched_path
                )
                released.append(c_name)
                logger.info(
                    f"[{service_name}] 防死锁：强制放行 {c_name}"
                )
                break

        self._start_torrent_ids(downloader, release_ids)

        # ── 8. 通知 ──
        if self._notify and (released or paused_by_overflow or skipped_disk):
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
            if skipped_disk:
                text_parts.append(
                    f"磁盘空间不足跳过 {len(skipped_disk)} 个: "
                    + ", ".join(skipped_disk[:5])
                )
            text_parts.append(
                f"当前活跃下载剩余: {StringUtils.str_filesize(active_left)} / "
                f"{self._max_capacity_gb} GB"
            )
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【qBittorrent 智能体积调度】",
                text="\n".join(text_parts),
            )

    def _fetch_torrents(self, downloader: Any) -> Tuple[Optional[List[dict]], Optional[str]]:
        if self._mponly:
            return downloader.get_torrents(tags=settings.TORRENT_TAG)
        return downloader.get_torrents()

    @staticmethod
    def _is_path_under_paths(save_path: str, paths: List[str]) -> bool:
        if not save_path or not paths:
            return False

        for base_path in paths:
            normalized_path = base_path.rstrip("/")
            if save_path == normalized_path or save_path.startswith(normalized_path + "/"):
                return True
        return False

    @staticmethod
    def _get_download_speed_bps(torrent: Dict[str, Any]) -> int:
        for field in ("dlspeed", "dl_speed", "download_speed"):
            speed = torrent.get(field)
            if isinstance(speed, (int, float)):
                return max(int(speed), 0)
        return 0

    def _is_low_speed_torrent(self, torrent: Dict[str, Any]) -> bool:
        if not self._enable_low_speed_tolerance:
            return False
        if self._low_speed_threshold_kib <= 0:
            return False
        if self._low_speed_stalled_only and torrent.get("state") != "stalledDL":
            return False
        threshold_bps = self._low_speed_threshold_kib * 1024
        speed_bps = self._get_download_speed_bps(torrent)
        return speed_bps <= threshold_bps

    @staticmethod
    def _stop_torrent_ids(downloader: Any, torrent_ids: List[str]):
        valid_ids = [torrent_id for torrent_id in torrent_ids if torrent_id]
        if valid_ids:
            downloader.stop_torrents(ids=valid_ids)

    @staticmethod
    def _start_torrent_ids(downloader: Any, torrent_ids: List[str]):
        valid_ids = [torrent_id for torrent_id in torrent_ids if torrent_id]
        if valid_ids:
            downloader.start_torrents(ids=valid_ids)

    def _get_free_space_map(self) -> Dict[str, int]:
        """
        获取各监控目录的当前磁盘剩余空间，返回 {路径: 剩余字节} 的 map。
        此 map 在放行过程中会被动态扣减，用于追踪"虚拟剩余空间"。
        """
        free_map: Dict[str, int] = {}
        if not self._download_paths:
            return free_map
        for dp in self._download_paths:
            free_bytes = SystemUtils.free_space(Path(dp))
            free_map[dp] = free_bytes
            logger.debug(f"磁盘空间: {dp} -> {StringUtils.str_filesize(free_bytes)}")
        return free_map

    def _match_download_path(self, save_path: str) -> Optional[str]:
        """
        将种子的 save_path 匹配到监控目录列表中对应的路径。
        返回匹配到的监控路径，未匹配到返回 None。
        """
        if not self._download_paths or not save_path:
            return None
        for dp in self._download_paths:
            if save_path == dp or save_path.startswith(dp.rstrip("/") + "/"):
                return dp
        return None

    def _check_disk_budget(
        self,
        save_path: str,
        needed: int,
        free_map: Dict[str, int],
        matched_path: Optional[str] = None,
    ) -> bool:
        """
        检查 save_path 所在磁盘能否容纳 needed 字节。
        基于虚拟剩余空间 map 判断：放行后剩余空间必须 >= min_free_gb。
        未配置监控目录或 save_path 不属于任何监控目录时，默认放行。
        """
        if not free_map:
            return True
        if matched_path is None:
            matched_path = self._match_download_path(save_path)
        if matched_path is None:
            return True  # save_path 不属于任何监控目录，跳过检查
        available = free_map.get(matched_path, 0)
        min_free_bytes = self._min_free_gb * (1024 ** 3)
        return (available - needed) >= min_free_bytes

    def _deduct_disk_budget(
        self,
        save_path: str,
        used: int,
        free_map: Dict[str, int],
        matched_path: Optional[str] = None,
    ):
        """
        从虚拟空间 map 中扣减已放行种子的体积。
        """
        if matched_path is None:
            matched_path = self._match_download_path(save_path)
        if matched_path is not None and matched_path in free_map:
            free_map[matched_path] -= used

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
                                                for config in self._downloader_helper
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
                    # ── 低速宽容 ──
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
                                            "model": "enable_low_speed_tolerance",
                                            "label": "低速种子宽容",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "low_speed_threshold_kib",
                                            "label": "低速阈值 (KiB/s)",
                                            "placeholder": "100",
                                            "type": "number",
                                            "hint": "溢出保护时优先保留低于该速度的种子",
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
                                            "model": "low_speed_stalled_only",
                                            "label": "仅 stalledDL 生效",
                                            "hint": "开启后仅对 stalledDL 状态应用低速宽容",
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
                                                "5. 磁盘保护：剩余空间低于阈值时仅暂停对应目录下载\n\n"
                                                "6. 低速宽容：溢出保护优先保留低速种子（可限定仅 stalledDL 生效）"
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
            "enable_low_speed_tolerance": True,
            "low_speed_threshold_kib": 100,
            "low_speed_stalled_only": False,
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
