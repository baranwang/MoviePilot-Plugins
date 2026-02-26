import threading
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

lock = threading.Lock()


class EmbyMissingSubscribe(_PluginBase):
    """扫描 Emby 媒体库中的遗漏剧集和电影合集，自动添加 MoviePilot 订阅"""

    # 插件名称
    plugin_name = "Emby 缺失订阅"
    # 插件描述
    plugin_desc = "扫描 Emby 媒体库中的遗漏剧集和电影合集（BoxSet），自动订阅缺失内容"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/icons/emby.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "baranwang"
    # 作者主页
    author_url = "https://github.com/baranwang"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "embymissingsubscribe_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = ""
    _skip_future: bool = True
    _enable_episodes: bool = True
    _enable_collections: bool = False

    # 运行时
    mediaserver_helper = None

    def init_plugin(self, config: dict = None):
        self._event = threading.Event()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._mediaservers: list = []
        self._libraries: list = []
        self._all_libraries: list = []
        self.mediaserver_helper = MediaServerHelper()
        self._media_chain = MediaChain()
        self._subscribe_chain = SubscribeChain()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron") or ""
            self._mediaservers = config.get("mediaservers") or []
            self._libraries = config.get("libraries") or []
            self._skip_future = config.get("skip_future", True)
            self._enable_episodes = config.get("enable_episodes", True)
            self._enable_collections = config.get("enable_collections", False)

        # 构建媒体库列表（供表单选择用）
        if self._mediaservers:
            self._all_libraries = self._build_library_list()

        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("Emby 缺失订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.scan_missing,
                trigger="date",
                run_date=datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + timedelta(seconds=3),
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "libraries": self._libraries,
                "skip_future": self._skip_future,
                "enable_episodes": self._enable_episodes,
                "enable_collections": self._enable_collections,
            })
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return True if self._enabled and self._cron and self._mediaservers else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/emby_missing",
                "event": EventType.PluginAction,
                "desc": "立即扫描 Emby 遗漏剧集和合集并订阅",
                "category": "Emby",
                "data": {"action": "emby_missing_subscribe"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state():
            return [
                {
                    "id": "EmbyMissingSubscribe",
                    "name": "Emby 缺失订阅",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.scan_missing,
                    "kwargs": {},
                }
            ]
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """
        处理远程命令
        """
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "emby_missing_subscribe":
                return
        logger.info("收到远程命令，立即执行 Emby 缺失扫描")
        self.scan_missing()

    # ================================================================
    # 核心扫描逻辑（统一入口）
    # ================================================================

    def scan_missing(self):
        """
        入口：遍历所有已配置的 Emby 服务器，按开关执行遗漏剧集和合集扫描
        """
        with lock:
            if not self._mediaservers:
                logger.warning("未配置媒体服务器")
                return

            if not self._enable_episodes and not self._enable_collections:
                logger.warning("遗漏剧集和合集订阅均未启用，跳过扫描")
                return

            services = self.mediaserver_helper.get_services(
                name_filters=self._mediaservers
            )
            if not services:
                logger.warning("获取媒体服务器实例失败，请检查配置")
                return

            # 加载历史记录（避免重复处理）
            history: dict = self.get_data("history") or {}
            total_added: List[str] = []

            for server_name, service in services.items():
                if service.instance.is_inactive():
                    logger.warning(f"媒体服务器 {server_name} 未连接，跳过")
                    continue
                if service.type != "emby":
                    logger.warning(
                        f"媒体服务器 {server_name} 不是 Emby 类型，跳过"
                    )
                    continue

                # —— 遗漏剧集扫描 ——
                if self._enable_episodes:
                    try:
                        added = self._scan_server_episodes(
                            server_name, service, history
                        )
                        total_added.extend(added)
                    except Exception as e:
                        logger.error(
                            f"扫描媒体服务器 {server_name} 遗漏剧集时出错: {e}"
                        )

                # —— 合集缺失电影扫描 ——
                if self._enable_collections:
                    try:
                        added = self._scan_server_collections(
                            server_name, service, history
                        )
                        total_added.extend(added)
                    except Exception as e:
                        logger.error(
                            f"扫描媒体服务器 {server_name} 合集时出错: {e}"
                        )

            # 持久化历史
            self.save_data("history", history)

            # 发送通知
            if self._notify and total_added:
                text_lines = [f"新增 {len(total_added)} 个订阅："]
                for item in total_added[:10]:
                    text_lines.append(f"· {item}")
                if len(total_added) > 10:
                    text_lines.append(f"... 等共 {len(total_added)} 个")
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Emby 缺失订阅】",
                    text="\n".join(text_lines),
                )

            if total_added:
                logger.info(
                    f"Emby 缺失扫描完成，新增 {len(total_added)} 个订阅"
                )
            else:
                logger.info("Emby 缺失扫描完成，无新增订阅")

    # ================================================================
    # 遗漏剧集扫描
    # ================================================================

    def _scan_server_episodes(
        self, server_name: str, service, history: dict
    ) -> List[str]:
        """
        扫描单个 Emby 服务器的遗漏剧集，返回本次新增订阅的描述列表
        """
        added_list: List[str] = []

        # 获取 Emby 用户 ID
        user_id = service.instance.user
        if not user_id:
            logger.warning(f"[{server_name}] 无法获取 Emby 用户 ID")
            return added_list

        # 确定要扫描的媒体库 ID
        library_ids = self._get_scan_library_ids(server_name)
        if not library_ids:
            # 不指定 ParentId，扫描全部媒体库
            library_ids = [None]

        for library_id in library_ids:
            try:
                items = self._fetch_missing_episodes(
                    service, user_id, library_id
                )
                if not items:
                    continue

                # 按 (SeriesId, Season) 分组
                groups = self._group_by_series_season(items)

                for (series_id, season), episodes in groups.items():
                    history_key = f"{server_name}:{series_id}:S{season}"
                    if history_key in history:
                        logger.debug(
                            f"[{server_name}] 已处理过: {history_key}"
                        )
                        continue

                    series_name = episodes[0].get("SeriesName", "未知")
                    ep_numbers = sorted(
                        ep.get("IndexNumber", 0) for ep in episodes
                    )

                    # 解析 TMDB ID
                    tmdb_id = self._resolve_tmdb_id(
                        service, user_id, series_id, series_name
                    )
                    if not tmdb_id:
                        logger.warning(
                            f"[{server_name}] 无法获取 TMDB ID: "
                            f"{series_name} S{season:02d}，跳过"
                        )
                        continue

                    # 获取年份
                    year = str(episodes[0].get("ProductionYear", ""))

                    # 创建订阅
                    sub_id, msg = self._subscribe_chain.add(
                        title=series_name,
                        year=year,
                        mtype=MediaType.TV,
                        tmdbid=tmdb_id,
                        season=season,
                        exist_ok=True,
                        username="Emby 遗漏集",
                    )

                    if sub_id:
                        desc = (
                            f"{series_name} S{season:02d} "
                            f"(TMDB:{tmdb_id}, 遗漏 {len(episodes)} 集)"
                        )
                        added_list.append(desc)
                        logger.info(f"[{server_name}] 订阅成功: {desc}")
                    else:
                        logger.info(
                            f"[{server_name}] 订阅跳过: "
                            f"{series_name} S{season:02d} - {msg}"
                        )

                    # 无论成功与否都记录历史，避免反复重试
                    history[history_key] = {
                        "type": "episode",
                        "series_name": series_name,
                        "season": season,
                        "tmdb_id": tmdb_id,
                        "episodes": ep_numbers,
                        "subscribe_id": sub_id,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }

            except Exception as e:
                logger.error(
                    f"[{server_name}] 扫描媒体库 {library_id} 遗漏剧集时出错: {e}"
                )

        return added_list

    # ================================================================
    # 合集缺失电影扫描
    # ================================================================

    def _scan_server_collections(
        self, server_name: str, service, history: dict
    ) -> List[str]:
        """
        扫描单个 Emby 服务器的电影合集（BoxSet），返回本次新增订阅的描述列表
        """
        added_list: List[str] = []

        user_id = service.instance.user
        if not user_id:
            logger.warning(f"[{server_name}] 无法获取 Emby 用户 ID")
            return added_list

        library_ids = self._get_scan_library_ids(server_name)
        if not library_ids:
            library_ids = [None]

        for library_id in library_ids:
            try:
                boxsets = self._fetch_boxsets(service, user_id, library_id)
                if not boxsets:
                    continue

                for boxset in boxsets:
                    added = self._process_boxset(
                        server_name, service, user_id, boxset, history
                    )
                    added_list.extend(added)

            except Exception as e:
                logger.error(
                    f"[{server_name}] 扫描媒体库 {library_id} 合集时出错: {e}"
                )

        return added_list

    def _process_boxset(
        self,
        server_name: str,
        service,
        user_id: str,
        boxset: dict,
        history: dict,
    ) -> List[str]:
        """
        处理单个 BoxSet：查询 TMDB 合集，对比已有电影，订阅缺失部分
        """
        added_list: List[str] = []
        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知合集")

        # 1. 获取 TMDB 合集 ID
        collection_id = self._resolve_collection_id(
            service, user_id, boxset
        )
        if not collection_id:
            logger.debug(
                f"[{server_name}] 合集 {boxset_name} "
                f"无法获取 TMDB 合集 ID，跳过"
            )
            return added_list

        # 2. 获取 TMDB 合集中的所有电影
        tmdb_movies = TmdbChain().tmdb_collection(
            collection_id=collection_id
        )
        if not tmdb_movies:
            logger.debug(
                f"[{server_name}] TMDB 合集 {collection_id} "
                f"({boxset_name}) 无电影信息"
            )
            return added_list

        logger.info(
            f"[{server_name}] 合集 {boxset_name} "
            f"(TMDB:{collection_id}) 共 {len(tmdb_movies)} 部电影"
        )

        # 3. 获取 BoxSet 中已有电影的 TMDB ID
        existing_tmdb_ids = self._get_boxset_movie_tmdb_ids(
            service, user_id, boxset_id
        )
        logger.debug(
            f"[{server_name}] 合集 {boxset_name} "
            f"已有 {len(existing_tmdb_ids)} 部电影"
        )

        # 4. 找出缺失的电影并订阅
        for movie in tmdb_movies:
            if not movie or not movie.tmdb_id:
                continue

            # 已在 Emby 媒体库中
            if movie.tmdb_id in existing_tmdb_ids:
                continue

            history_key = (
                f"{server_name}:collection:{collection_id}"
                f":movie:{movie.tmdb_id}"
            )
            if history_key in history:
                logger.debug(
                    f"[{server_name}] 已处理过: "
                    f"{movie.title} (TMDB:{movie.tmdb_id})"
                )
                continue

            # 创建订阅
            sid, msg = SubscribeChain().add(
                title=movie.title,
                year=movie.year,
                mtype=MediaType.MOVIE,
                tmdbid=movie.tmdb_id,
                exist_ok=True,
                username="Emby 合集订阅",
                message=False,
            )

            if sid:
                desc = (
                    f"{movie.title} ({movie.year}) "
                    f"(TMDB:{movie.tmdb_id}, 合集: {boxset_name})"
                )
                added_list.append(desc)
                logger.info(f"[{server_name}] 订阅成功: {desc}")
            else:
                logger.info(
                    f"[{server_name}] 订阅跳过: "
                    f"{movie.title} ({movie.year}) - {msg}"
                )

            # 无论成功与否都记录历史，避免反复重试
            history[history_key] = {
                "type": "collection",
                "collection_name": boxset_name,
                "collection_id": collection_id,
                "movie_title": movie.title,
                "movie_year": movie.year,
                "tmdb_id": movie.tmdb_id,
                "subscribe_id": sid,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        return added_list

    # ================================================================
    # Emby API 交互
    # ================================================================

    def _fetch_missing_episodes(
        self, service, user_id: str, parent_id: Optional[str] = None
    ) -> List[dict]:
        """
        调用 Emby /Shows/Missing API 获取遗漏（Virtual）剧集
        """
        url = (
            f"[HOST]emby/Shows/Missing?"
            f"api_key=[APIKEY]"
            f"&UserId={user_id}"
            f"&Fields=ProviderIds,Overview,PremiereDate,ProductionYear"
            f"&Limit=500"
        )
        if parent_id:
            url += f"&ParentId={parent_id}"

        try:
            res = service.instance.get_data(url=url)
            if not res:
                return []
            data = res.json()
            items = data.get("Items", [])

            # 过滤尚未播出的剧集
            if self._skip_future and items:
                now = datetime.now()
                filtered = []
                for item in items:
                    premiere = item.get("PremiereDate")
                    if premiere:
                        try:
                            premiere_dt = datetime.fromisoformat(
                                premiere.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            if premiere_dt > now:
                                continue
                        except (ValueError, TypeError):
                            pass
                    filtered.append(item)
                items = filtered

            logger.info(
                f"获取到 {len(items)} 个遗漏剧集"
                + (f" (媒体库 {parent_id})" if parent_id else "")
            )
            return items

        except Exception as e:
            logger.error(f"获取遗漏剧集失败: {e}")
            return []

    def _fetch_boxsets(
        self,
        service,
        user_id: str,
        parent_id: Optional[str] = None,
    ) -> List[dict]:
        """
        获取 Emby 媒体库中的所有 BoxSet（合集）
        """
        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&IncludeItemTypes=BoxSet"
            f"&Recursive=true"
            f"&Fields=ProviderIds"
            f"&Limit=500"
        )
        if parent_id:
            url += f"&ParentId={parent_id}"

        try:
            res = service.instance.get_data(url=url)
            if not res:
                return []
            data = res.json()
            items = data.get("Items", [])
            logger.info(
                f"获取到 {len(items)} 个合集"
                + (f" (媒体库 {parent_id})" if parent_id else "")
            )
            return items
        except Exception as e:
            logger.error(f"获取合集列表失败: {e}")
            return []

    def _get_boxset_movie_tmdb_ids(
        self, service, user_id: str, boxset_id: str
    ) -> set:
        """
        获取 BoxSet 中已有电影的 TMDB ID 集合
        """
        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Recursive=true"
            f"&Limit=500"
        )
        tmdb_ids: set = set()
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return tmdb_ids
            data = res.json()
            for item in data.get("Items", []):
                provider_ids = item.get("ProviderIds", {})
                tmdb_str = (
                    provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                )
                if tmdb_str:
                    try:
                        tmdb_ids.add(int(tmdb_str))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.error(f"获取合集子项失败: {e}")
        return tmdb_ids

    def _resolve_tmdb_id(
        self,
        service,
        user_id: str,
        series_id: str,
        series_name: str,
    ) -> Optional[int]:
        """
        获取剧集的 TMDB ID：
        1. 优先从 Emby Series 级别的 ProviderIds 中获取
        2. 回退到 MediaChain 按标题识别
        """
        # —— 方式 1: Emby Series 元数据 ——
        try:
            url = (
                f"[HOST]emby/Users/{user_id}/Items/{series_id}?"
                f"api_key=[APIKEY]"
                f"&Fields=ProviderIds"
            )
            res = service.instance.get_data(url=url)
            if res:
                series_data = res.json()
                provider_ids = series_data.get("ProviderIds", {})
                tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                if tmdb_str:
                    return int(tmdb_str)
        except Exception as e:
            logger.debug(f"从 Emby 获取 Series TMDB ID 失败: {e}")

        # —— 方式 2: MediaChain 识别 ——
        try:
            meta = MetaInfo(title=series_name)
            meta.type = MediaType.TV
            mediainfo = self._media_chain.recognize_media(meta=meta)
            if mediainfo and mediainfo.tmdb_id:
                logger.info(
                    f"通过 MediaChain 识别到 TMDB ID: "
                    f"{series_name} -> {mediainfo.tmdb_id}"
                )
                return mediainfo.tmdb_id
        except Exception as e:
            logger.debug(f"MediaChain 识别失败: {series_name}, {e}")

        return None

    def _resolve_collection_id(
        self, service, user_id: str, boxset: dict
    ) -> Optional[int]:
        """
        获取 BoxSet 对应的 TMDB 合集 ID：
        1. 优先从 BoxSet 自身的 ProviderIds 中获取
        2. 回退到子项电影的 belongs_to_collection 字段
        """
        # —— 方式 1: BoxSet 的 ProviderIds ——
        provider_ids = boxset.get("ProviderIds", {})
        tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
        if tmdb_str:
            try:
                return int(tmdb_str)
            except (ValueError, TypeError):
                pass

        # —— 方式 2: 从子项电影中获取 ——
        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知")
        if not boxset_id:
            return None

        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Limit=1"
        )
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return None
            data = res.json()
            items = data.get("Items", [])
            if not items:
                return None

            # 取第一部电影的 TMDB ID
            movie_item = items[0]
            movie_provider_ids = movie_item.get("ProviderIds", {})
            movie_tmdb_str = (
                movie_provider_ids.get("Tmdb")
                or movie_provider_ids.get("tmdb")
            )
            if not movie_tmdb_str:
                return None

            movie_tmdb_id = int(movie_tmdb_str)

            # 通过 MediaChain 获取电影详情，提取 collection_id
            mediainfo = MediaChain().recognize_media(
                mtype=MediaType.MOVIE, tmdbid=movie_tmdb_id
            )
            if not mediainfo:
                return None

            # 尝试从 collection_id 属性获取
            cid = getattr(mediainfo, "collection_id", None)
            if cid:
                logger.info(
                    f"通过子项电影 {movie_item.get('Name')} "
                    f"识别到合集 {boxset_name} 的 TMDB 合集 ID: {cid}"
                )
                return cid

            # 尝试从 tmdb_info 中的 belongs_to_collection 获取
            if mediainfo.tmdb_info:
                btc = mediainfo.tmdb_info.get("belongs_to_collection")
                if btc and btc.get("id"):
                    cid = btc["id"]
                    logger.info(
                        f"通过子项电影 {movie_item.get('Name')} "
                        f"的 belongs_to_collection 识别到"
                        f"合集 {boxset_name} 的 TMDB 合集 ID: {cid}"
                    )
                    return cid

        except Exception as e:
            logger.debug(
                f"从子项电影获取 TMDB 合集 ID 失败: {e}"
            )

        return None

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _group_by_series_season(
        items: List[dict],
    ) -> Dict[Tuple[str, int], List[dict]]:
        """
        将遗漏剧集按 (SeriesId, Season) 分组
        """
        groups: Dict[Tuple[str, int], List[dict]] = {}
        for item in items:
            series_id = item.get("SeriesId")
            season = item.get("ParentIndexNumber", 1)
            if not series_id:
                continue
            key = (series_id, season)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return groups

    def _get_scan_library_ids(self, server_name: str) -> List[str]:
        """
        从用户选择的媒体库中，提取属于指定服务器的媒体库 ID
        """
        if not self._libraries:
            return []

        result = []
        prefix = f"{server_name}-"
        for lib_value in self._libraries:
            if lib_value.startswith(prefix):
                lib_id = lib_value[len(prefix):]
                result.append(lib_id)
        return result

    def _build_library_list(self) -> list:
        """
        构建媒体库选项列表（供表单多选使用）
        """
        lib_items = []
        if not self._mediaservers:
            return lib_items

        services = self.mediaserver_helper.get_services(
            name_filters=self._mediaservers
        )
        if not services:
            return lib_items

        for server_name, service in services.items():
            if service.instance.is_inactive() or service.type != "emby":
                continue
            try:
                url = (
                    "[HOST]emby/Library/VirtualFolders/Query?"
                    "api_key=[APIKEY]"
                )
                res = service.instance.get_data(url=url)
                if not res:
                    continue
                data = res.json()
                libraries = data.get("Items", [])
                for lib in libraries:
                    lib_id = lib.get("Id")
                    lib_name = lib.get("Name")
                    if lib_id and lib_name:
                        lib_items.append({
                            "title": f"{server_name}: {lib_name}",
                            "value": f"{server_name}-{lib_id}",
                        })
            except Exception as e:
                logger.debug(
                    f"获取媒体库列表失败: {server_name}, {e}"
                )

        return lib_items

    # ================================================================
    # 表单
    # ================================================================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 构建媒体服务器选项
        server_items = []
        if self.mediaserver_helper:
            for svc in self.mediaserver_helper.get_services().values():
                server_items.append({
                    "title": svc.name,
                    "value": svc.name,
                })

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
                    # ── 功能开关行 ──
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
                                            "model": "enable_episodes",
                                            "label": "订阅遗漏剧集",
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
                                            "model": "enable_collections",
                                            "label": "订阅合集缺失电影",
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
                                            "model": "skip_future",
                                            "label": "跳过未播出剧集",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 执行周期 ──
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
                                            "placeholder": "0 8 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # ── 媒体服务器 + 媒体库 ──
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
                                            "model": "mediaservers",
                                            "label": "媒体服务器",
                                            "items": server_items,
                                            "hint": "选择要扫描的 Emby 服务器",
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
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "libraries",
                                            "label": "媒体库",
                                            "items": self._all_libraries
                                            or [],
                                            "hint": "选择要扫描的媒体库，不选则扫描全部",
                                            "persistentHint": True,
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
                                                "本插件支持两种扫描模式"
                                                "，可独立开关：\n\n"
                                                "【遗漏剧集】调用 Emby"
                                                " /Shows/Missing API"
                                                " 获取遗漏剧集，"
                                                "按剧集 + 季创建"
                                                " MoviePilot 订阅\n\n"
                                                "【合集缺失电影】扫描"
                                                " Emby 中的 BoxSet"
                                                " 合集，通过 TMDB"
                                                " 获取合集完整电影列表"
                                                "，自动订阅缺失的电影"
                                                "\n\n"
                                                "两种模式均会记录历史"
                                                "避免重复处理，已有订阅"
                                                "不会重复添加"
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
            "cron": "",
            "mediaservers": [],
            "libraries": [],
            "skip_future": True,
            "enable_episodes": True,
            "enable_collections": False,
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
            logger.error(f"Emby 缺失订阅停止服务异常: {e}")
