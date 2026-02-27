import hashlib
import time
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import grpc
from google.protobuf import empty_pb2

try:
    from . import clouddrive_pb2 as CloudDrive_pb2
    from . import clouddrive_pb2_grpc as CloudDrive_pb2_grpc
except Exception:
    import clouddrive_pb2 as CloudDrive_pb2
    import clouddrive_pb2_grpc as CloudDrive_pb2_grpc

from app.core.config import settings
from app.log import logger
from app.schemas import FileItem, StorageUsage


class Cd2Api:
    """
    CloudDrive2 gRPC 操作类（按官方 proto 直接调用）
    """

    def __init__(self, cd2_url: str, api_key: str, disk_name: str):
        self._disk_name = disk_name
        self._channel = None

        parsed = urlsplit(cd2_url)
        scheme = parsed.scheme or "http"
        host = parsed.netloc or parsed.path
        if not host:
            host = "127.0.0.1:19798"
        self._origin_scheme = scheme
        self._origin_host = host

        self._channel = grpc.insecure_channel(host, options=[
            ('grpc.max_send_message_length', 20 * 1024 * 1024),
            ('grpc.max_receive_message_length', 20 * 1024 * 1024),
            ('grpc.keepalive_time_ms', 30_000),
            ('grpc.keepalive_timeout_ms', 10_000),
            ('grpc.keepalive_permit_without_calls', 1),
            ('grpc.http2.max_pings_without_data', 0),
        ])
        try:
            self._stub = CloudDrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)
            token = self._normalize_api_key(api_key)
            if not token:
                raise RuntimeError("CloudDrive2 API key 不能为空")
            self._api_key = token
            self._metadata_candidates: List[Tuple[str, List[Tuple[str, str]]]] = self._build_metadata_candidates(token)
            self._active_metadata_index = 0
            self._metadata: List[Tuple[str, str]] = self._metadata_candidates[self._active_metadata_index][1]
            self._token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]
            self._token_length = len(token)
            self._token_root = "/"
            self._token_info_state = "unknown"
            self._token_info_name = ""
            self._token_allow_list_count: Optional[int] = None
            self._auth_failed_logged = False
            self._probe_system_info()
            self._init_token_root()
            self._preflight_authorized_access()
        except Exception:
            self.close()
            raise

    def __del__(self):
        self.close()

    @staticmethod
    def _normalize_api_key(api_key: str) -> str:
        token = (api_key or "").strip().strip('"').strip("'")
        token = token.replace("\r", "").replace("\n", "").strip()
        token = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", token)
        if token.lower().startswith("authorization:"):
            token = token.split(":", 1)[1].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        token = "".join(token.split())
        return token

    def _probe_system_info(self):
        try:
            self._stub.GetSystemInfo(empty_pb2.Empty())
        except grpc.RpcError as e:
            raise RuntimeError(
                f"CloudDrive2 服务不可用或地址不正确: {self._origin_host}, {self._rpc_error_text(e)}"
            ) from e

    @staticmethod
    def _build_metadata_candidates(token: str) -> List[Tuple[str, List[Tuple[str, str]]]]:
        candidates = [
            ("authorization:Bearer", [("authorization", f"Bearer {token}")]),
        ]
        unique: List[Tuple[str, List[Tuple[str, str]]]] = []
        seen = set()
        for label, metadata in candidates:
            key = tuple(metadata)
            if key in seen:
                continue
            seen.add(key)
            unique.append((label, metadata))
        return unique

    def _set_active_metadata(self, index: int):
        if index == self._active_metadata_index:
            return
        self._active_metadata_index = index
        variant, metadata = self._metadata_candidates[index]
        self._metadata = metadata
        logger.info(f"【Cd2Disk】已切换鉴权头格式 variant={variant}, token_fp={self._token_fingerprint}")

    def _call_authed(self, method_name: str, request: Any):
        rpc = getattr(self._stub, method_name)
        last_unauth = None

        for index, (_, metadata) in enumerate(self._metadata_candidates):
            try:
                response = rpc(request, metadata=metadata)
                self._set_active_metadata(index)
                return response
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    last_unauth = e
                    continue
                raise

        if last_unauth:
            raise last_unauth
        raise RuntimeError(f"CloudDrive2 调用失败: {method_name}")

    def _init_token_root(self):
        try:
            token_info = self._stub.GetApiTokenInfo(CloudDrive_pb2.StringValue(value=self._api_key))
            self._token_info_state = "ok"
            info_token = self._normalize_api_key(getattr(token_info, "token", "") or "")
            info_name = (getattr(token_info, "friendly_name", "") or "").strip()
            self._token_info_name = info_name
            if not info_token and not info_name:
                logger.warning(
                    "【Cd2Disk】GetApiTokenInfo 未返回有效 token 信息，可能不是当前实例创建的 token"
                )

            permissions = getattr(token_info, "permissions", None)
            if permissions and hasattr(permissions, "allow_list") and not permissions.allow_list:
                logger.warning("【Cd2Disk】当前 API key 未授予目录读取权限 allow_list，插件将无法浏览文件")
            if permissions and hasattr(permissions, "allow_list"):
                try:
                    self._token_allow_list_count = len(permissions.allow_list)
                except Exception:
                    self._token_allow_list_count = None

            token_root = self._normalize_path(getattr(token_info, "rootDir", "") or "/")
            if token_root != "/":
                token_root = token_root.rstrip("/")
            self._token_root = token_root or "/"
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._token_info_state = "unauthenticated"
                logger.warning(
                    "【Cd2Disk】无法通过 GetApiTokenInfo 校验当前 token，请确认该 key 在当前实例有效"
                )
                return
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                self._token_info_state = "unimplemented"
                logger.debug("【Cd2Disk】当前 CloudDrive2 版本未提供 GetApiTokenInfo，空间统计将回退到根路径")
                return
            self._token_info_state = "error"
            logger.debug(f"【Cd2Disk】读取 API key rootDir 失败，回退到 '/': {e}")
        except Exception as e:
            self._token_info_state = "error"
            logger.debug(f"【Cd2Disk】读取 API key rootDir 失败，回退到 '/': {e}")

    def _preflight_authorized_access(self):
        try:
            self._call_authed("GetAccountStatus", empty_pb2.Empty())
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise RuntimeError(
                    f"CloudDrive2 鉴权失败: endpoint={self._origin_host}, token_fp={self._token_fingerprint}, "
                    f"token_info={self._token_info_state}, 请确认 token 来自当前实例且未过期未删除"
                ) from e
            logger.debug(f"【Cd2Disk】账户状态预检跳过: {self._rpc_error_text(e)}")

        target = self._token_root or "/"
        try:
            self._list_cloud_files(target, force_refresh=False)
        except grpc.RpcError as e:
            code = e.code()
            if code == grpc.StatusCode.UNAUTHENTICATED:
                raise RuntimeError(
                    f"CloudDrive2 鉴权失败: endpoint={self._origin_host}, token_fp={self._token_fingerprint}, "
                    f"token_info={self._token_info_state}, 请确认 token 来自当前实例且未过期未删除"
                ) from e
            if code == grpc.StatusCode.PERMISSION_DENIED:
                raise RuntimeError(
                    f"CloudDrive2 API key 缺少目录读取权限 allow_list: root={target}, "
                    f"allow_list_count={self._token_allow_list_count}"
                ) from e
            logger.debug(f"【Cd2Disk】初始化预检跳过: {self._rpc_error_text(e)}")

    def _to_cloud_path(self, path: str) -> str:
        normalized = self._normalize_path(path)
        if self._token_root == "/":
            return normalized
        if normalized == "/":
            return self._token_root
        if normalized == self._token_root or normalized.startswith(f"{self._token_root}/"):
            return normalized
        return self._join_path(self._token_root, normalized.lstrip("/"))

    @staticmethod
    def _rpc_error_text(error: grpc.RpcError) -> str:
        code = error.code()
        details = error.details() if hasattr(error, "details") else str(error)
        return f"{getattr(code, 'name', 'UNKNOWN')}: {details}"

    def _log_auth_error_once(self, action: str, target: str, error: grpc.RpcError):
        if not self._auth_failed_logged:
            logger.error(
                f"【Cd2Disk】CloudDrive2 鉴权失败，请检查 API key 或权限设置 "
                f"(endpoint={self._origin_host}, token_fp={self._token_fingerprint}, token_len={self._token_length})"
            )
            logger.error("【Cd2Disk】请确认 token 来自当前 CloudDrive2 实例，且未过期、未删除")
            self._auth_failed_logged = True
        logger.debug(f"【Cd2Disk】{action}失败: {target}, {self._rpc_error_text(error)}")

    @staticmethod
    def _normalize_path(path: str) -> str:
        if not path:
            return "/"
        value = path.replace("\\", "/")
        if not value.startswith("/"):
            value = f"/{value}"
        value = str(PurePosixPath(value))
        if not value.startswith("/"):
            value = f"/{value}"
        return value

    def _normalize_dir_path(self, path: str) -> str:
        value = self._to_cloud_path(path)
        if value != "/":
            value = value.rstrip("/")
        return value

    def _normalize_file_path(self, path: str) -> str:
        value = self._to_cloud_path(path)
        if value != "/":
            value = value.rstrip("/")
        return value

    @staticmethod
    def _join_path(parent_path: str, name: str) -> str:
        parent = str(PurePosixPath(parent_path))
        if parent in ("", "."):
            parent = "/"
        if parent == "/":
            return f"/{name}"
        return f"{parent}/{name}"

    @staticmethod
    def _timestamp_to_int(timestamp: Any) -> Optional[int]:
        if not timestamp:
            return None
        seconds = getattr(timestamp, "seconds", None)
        if seconds is None:
            return None
        try:
            value = int(seconds)
            return value if value > 0 else None
        except Exception:
            return None

    def _to_file_item(self, cloud_file: Any) -> FileItem:
        is_dir = bool(getattr(cloud_file, "isDirectory", False))
        raw_path = getattr(cloud_file, "fullPathName", None) or "/"
        file_path = self._normalize_path(str(raw_path))
        if is_dir and file_path != "/":
            file_path = f"{file_path.rstrip('/')}/"

        name = getattr(cloud_file, "name", "")
        if not name:
            if file_path == "/":
                name = "/"
            else:
                name = PurePosixPath(file_path.rstrip("/")).name

        pure_name = PurePosixPath(name)
        basename = name if is_dir else pure_name.stem
        extension = None if is_dir else (pure_name.suffix[1:] if pure_name.suffix else None)

        normalized_no_suffix = file_path if file_path == "/" else file_path.rstrip("/")
        fileid = getattr(cloud_file, "id", "") or normalized_no_suffix

        parent_fileid = None
        if normalized_no_suffix != "/":
            parent_path = str(PurePosixPath(normalized_no_suffix).parent)
            if parent_path in ("", "."):
                parent_path = "/"
            parent_fileid = parent_path

        size = None
        if not is_dir:
            try:
                size = int(getattr(cloud_file, "size", 0) or 0)
            except Exception:
                size = None

        modify_time = self._timestamp_to_int(getattr(cloud_file, "writeTime", None))
        if modify_time is None:
            modify_time = self._timestamp_to_int(getattr(cloud_file, "createTime", None))

        return FileItem(
            storage=self._disk_name,
            fileid=str(fileid),
            parent_fileid=parent_fileid,
            name=name,
            basename=basename,
            extension=extension,
            type="dir" if is_dir else "file",
            path=file_path,
            size=size,
            modify_time=modify_time,
        )

    def _root_item(self) -> FileItem:
        return FileItem(
            storage=self._disk_name,
            fileid="/",
            parent_fileid=None,
            name="/",
            basename="/",
            extension=None,
            type="dir",
            path="/",
            size=None,
            modify_time=None,
        )

    @staticmethod
    def _is_success(resp: Any) -> bool:
        if resp is None:
            return False
        if hasattr(resp, "success"):
            return bool(getattr(resp, "success"))
        if hasattr(resp, "result") and hasattr(resp.result, "success"):
            return bool(resp.result.success)
        return True

    @staticmethod
    def _result_paths(resp: Any) -> List[str]:
        paths = getattr(resp, "resultFilePaths", None)
        if not paths:
            return []
        return list(paths)

    def _list_cloud_files(self, path: str, force_refresh: bool = False) -> List[Any]:
        req = CloudDrive_pb2.ListSubFileRequest(path=path, forceRefresh=force_refresh)
        last_unauth = None

        for index, (_, metadata) in enumerate(self._metadata_candidates):
            result: List[Any] = []
            try:
                for reply in self._stub.GetSubFiles(req, metadata=metadata):
                    for sub_file in reply.subFiles:
                        result.append(sub_file)
                self._set_active_metadata(index)
                return result
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    last_unauth = e
                    continue
                raise

        if last_unauth:
            raise last_unauth
        return []

    def _resolve_download_url(self, path: str) -> tuple[str, Dict[str, str]]:
        req = CloudDrive_pb2.GetDownloadUrlPathRequest(
            path=path,
            preview=False,
            lazy_read=False,
            get_direct_url=True,
        )
        info = self._call_authed("GetDownloadUrlPath", req)

        headers: Dict[str, str] = {}
        user_agent = getattr(info, "userAgent", "")
        if user_agent:
            headers["User-Agent"] = user_agent
        elif getattr(settings, "USER_AGENT", None):
            headers["User-Agent"] = settings.USER_AGENT

        additional_headers = getattr(info, "additionalHeaders", None)
        if additional_headers:
            for key, value in additional_headers.items():
                headers[str(key)] = str(value)

        direct_url = getattr(info, "directUrl", "")
        if direct_url:
            return direct_url, headers

        download_url_path = getattr(info, "downloadUrlPath", "")
        if not download_url_path:
            raise RuntimeError("CloudDrive2 未返回下载地址")

        filled_path = (
            str(download_url_path)
            .replace("{SCHEME}", self._origin_scheme)
            .replace("{HOST}", self._origin_host)
            .replace("{PREVIEW}", "false")
        )
        if not filled_path.startswith("/"):
            filled_path = f"/{filled_path}"

        return f"{self._origin_scheme}://{self._origin_host}{filled_path}", headers

    def list(self, fileitem: FileItem) -> List[FileItem]:
        if fileitem.type == "file":
            item = self.detail(fileitem)
            return [item] if item else []

        path = self._normalize_dir_path(fileitem.path)
        try:
            files = self._list_cloud_files(path, force_refresh=False)
            return [self._to_file_item(one) for one in files]
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._log_auth_error_once("浏览目录", path, e)
            else:
                logger.error(f"【Cd2Disk】浏览目录失败: {path}, {self._rpc_error_text(e)}")
            return []
        except Exception as e:
            logger.error(f"【Cd2Disk】浏览目录失败: {path}, {e}")
            return []

    def iter_files(self, fileitem: FileItem) -> Optional[List[FileItem]]:
        if fileitem.type == "file":
            item = self.detail(fileitem)
            return [item] if item else []

        root = self._normalize_dir_path(fileitem.path)
        result: List[FileItem] = []
        pending: List[str] = [root]
        visited = set()

        try:
            while pending:
                current = pending.pop(0)
                if current in visited:
                    continue
                visited.add(current)

                for cloud_file in self._list_cloud_files(current, force_refresh=False):
                    item = self._to_file_item(cloud_file)
                    result.append(item)
                    if item.type == "dir":
                        pending.append(self._normalize_dir_path(item.path))
            return result
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._log_auth_error_once("递归遍历目录", root, e)
            else:
                logger.error(f"【Cd2Disk】递归遍历目录失败: {root}, {self._rpc_error_text(e)}")
            return None
        except Exception as e:
            logger.error(f"【Cd2Disk】递归遍历目录失败: {root}, {e}")
            return None

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        parent_path = self._normalize_dir_path(fileitem.path)
        target_path = self._join_path(parent_path, name)

        try:
            req = CloudDrive_pb2.CreateFolderRequest(parentPath=parent_path, folderName=name)
            resp = self._call_authed("CreateFolder", req)
            if hasattr(resp, "result") and not self._is_success(resp.result):
                error_msg = getattr(resp.result, "errorMessage", "")
                logger.error(f"【Cd2Disk】创建目录失败: {target_path}, {error_msg}")
                return None

            folder = getattr(resp, "folderCreated", None)
            if folder and (getattr(folder, "fullPathName", None) or getattr(folder, "name", None)):
                return self._to_file_item(folder)
            return self.get_item(Path(target_path))
        except Exception as e:
            logger.error(f"【Cd2Disk】创建目录失败: {target_path}, {e}")
            return None

    def get_item(self, path: Path) -> Optional[FileItem]:
        target_path = self._normalize_path(path.as_posix())
        if target_path == "/":
            return self._root_item()

        try:
            req = CloudDrive_pb2.FindFileByPathRequest(path=self._normalize_file_path(target_path))
            cloud_file = self._call_authed("FindFileByPath", req)
            if not getattr(cloud_file, "name", "") and not getattr(cloud_file, "fullPathName", ""):
                return None
            return self._to_file_item(cloud_file)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                self._log_auth_error_once("获取文件详情", target_path, e)
            return None
        except Exception:
            return None

    def get_parent(self, fileitem: FileItem) -> Optional[FileItem]:
        src_path = self._normalize_file_path(fileitem.path)
        if src_path == "/":
            return None
        parent = str(PurePosixPath(src_path).parent)
        if parent in ("", "."):
            parent = "/"
        return self.get_item(Path(parent))

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        return self.get_item(Path(fileitem.path))

    def get_folder(self, path: Path) -> Optional[FileItem]:
        """
        获取目录信息，不存在则逐级创建。
        供 MoviePilot 媒体整理调用。
        """
        target = self._normalize_path(path.as_posix())

        # 先查询是否已存在
        item = self.get_item(path)
        if item and item.type == "dir":
            return item

        # 不存在，逐级创建
        parts = PurePosixPath(target).parts  # ('/', 'a', 'b', 'c')
        current = "/"
        current_item: Optional[FileItem] = None
        for part in parts[1:]:  # 跳过根 '/'
            next_path = self._join_path(current, part)
            existing = self.get_item(Path(next_path))
            if existing and existing.type == "dir":
                current = next_path
                current_item = existing
                continue

            # 需要创建这一级目录
            parent_item = current_item or self._root_item()
            created = self.create_folder(parent_item, part)
            if not created:
                logger.error(f"【Cd2Disk】创建目录失败: {next_path}")
                return None
            current = next_path
            current_item = created

        return current_item

    def delete(self, fileitem: FileItem) -> bool:
        path = self._normalize_path(fileitem.path)
        try:
            resp = self._call_authed("DeleteFile", CloudDrive_pb2.FileRequest(path=path))
            if not self._is_success(resp):
                logger.error(f"【Cd2Disk】删除失败: {path}, {getattr(resp, 'errorMessage', '')}")
                return False
            return True
        except Exception as e:
            logger.error(f"【Cd2Disk】删除失败: {path}, {e}")
            return False

    def rename(self, fileitem: FileItem, name: str) -> bool:
        src_path = self._normalize_file_path(fileitem.path)
        if src_path == "/":
            return False

        try:
            req = CloudDrive_pb2.RenameFileRequest(theFilePath=src_path, newName=name)
            resp = self._call_authed("RenameFile", req)
            if not self._is_success(resp):
                logger.error(
                    f"【Cd2Disk】重命名失败: {src_path} -> {name}, {getattr(resp, 'errorMessage', '')}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"【Cd2Disk】重命名失败: {src_path} -> {name}, {e}")
            return False

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        src_path = self._normalize_file_path(fileitem.path)
        dst_dir = self._normalize_dir_path(path.as_posix())
        src_name = PurePosixPath(src_path).name
        target_name = new_name or src_name

        try:
            req = CloudDrive_pb2.MoveFileRequest(
                theFilePaths=[src_path],
                destPath=dst_dir,
                conflictPolicy=CloudDrive_pb2.MoveFileRequest.Overwrite,
            )
            resp = self._call_authed("MoveFile", req)
            if not self._is_success(resp):
                logger.error(
                    f"【Cd2Disk】移动失败: {src_path} -> {dst_dir}, {getattr(resp, 'errorMessage', '')}"
                )
                return False

            if target_name != src_name:
                result_paths = self._result_paths(resp)
                moved_path = result_paths[0] if result_paths else self._join_path(dst_dir, src_name)
                rename_resp = self._call_authed(
                    "RenameFile",
                    CloudDrive_pb2.RenameFileRequest(theFilePath=moved_path, newName=target_name),
                )
                if not self._is_success(rename_resp):
                    logger.error(
                        f"【Cd2Disk】移动后重命名失败: {moved_path} -> {target_name}, "
                        f"{getattr(rename_resp, 'errorMessage', '')}"
                    )
                    return False
            return True
        except Exception as e:
            logger.error(f"【Cd2Disk】移动失败: {src_path} -> {dst_dir}, {e}")
            return False

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        src_path = self._normalize_file_path(fileitem.path)
        dst_dir = self._normalize_dir_path(path.as_posix())
        src_name = PurePosixPath(src_path).name
        target_name = new_name or src_name

        try:
            req = CloudDrive_pb2.CopyFileRequest(
                theFilePaths=[src_path],
                destPath=dst_dir,
                conflictPolicy=CloudDrive_pb2.CopyFileRequest.Overwrite,
            )
            resp = self._call_authed("CopyFile", req)
            if not self._is_success(resp):
                logger.error(
                    f"【Cd2Disk】复制失败: {src_path} -> {dst_dir}, {getattr(resp, 'errorMessage', '')}"
                )
                return False

            if target_name != src_name:
                result_paths = self._result_paths(resp)
                copied_path = result_paths[0] if result_paths else self._join_path(dst_dir, src_name)
                rename_resp = self._call_authed(
                    "RenameFile",
                    CloudDrive_pb2.RenameFileRequest(theFilePath=copied_path, newName=target_name),
                )
                if not self._is_success(rename_resp):
                    logger.error(
                        f"【Cd2Disk】复制后重命名失败: {copied_path} -> {target_name}, "
                        f"{getattr(rename_resp, 'errorMessage', '')}"
                    )
                    return False
            return True
        except Exception as e:
            logger.error(f"【Cd2Disk】复制失败: {src_path} -> {dst_dir}, {e}")
            return False

    def download(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        if fileitem.type != "file":
            return None

        remote_path = self._normalize_file_path(fileitem.path)
        local_path = path or settings.TEMP_PATH / fileitem.name
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            url, headers = self._resolve_download_url(remote_path)
            req = Request(url=url, headers=headers)
            with urlopen(req) as resp, open(local_path, "wb") as f:
                while True:
                    chunk = resp.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            return local_path
        except Exception as e:
            logger.error(f"【Cd2Disk】下载失败: {remote_path} -> {local_path}, {e}")
            if local_path.exists():
                local_path.unlink()
            return None

    def upload(
        self,
        target_dir: FileItem,
        local_path: Path,
        new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        if not local_path.exists() or not local_path.is_file():
            logger.error(f"【Cd2Disk】上传失败，本地文件不存在: {local_path}")
            return None

        target_name = new_name or local_path.name
        remote_dir = self._normalize_dir_path(target_dir.path)
        remote_path = self._join_path(remote_dir, target_name)

        # 确保目标目录存在，不存在则递归创建
        if not self.get_folder(Path(remote_dir)):
            logger.error(f"【Cd2Disk】上传失败，目标目录创建失败: {remote_dir}")
            return None

        existed = self.get_item(Path(remote_path))
        if existed and not self.delete(existed):
            logger.error(f"【Cd2Disk】上传失败，无法覆盖已有文件: {remote_path}")
            return None

        file_handle = 0
        try:
            try:
                create_resp = self._call_authed(
                    "CreateFile",
                    CloudDrive_pb2.CreateFileRequest(parentPath=remote_dir, fileName=target_name),
                )
            except Exception as e:
                logger.error(f"【Cd2Disk】上传失败 [CreateFile]: {remote_path}, {e}")
                return None

            file_handle = int(getattr(create_resp, "fileHandle", 0) or 0)
            if file_handle <= 0:
                logger.error(f"【Cd2Disk】上传失败，创建远端文件句柄失败: {remote_path}")
                return None

            offset = 0
            with open(local_path, "rb") as f:
                while True:
                    data = f.read(3 * 1024 * 1024)
                    if not data:
                        break
                    try:
                        write_resp = self._call_authed(
                            "WriteToFile",
                            CloudDrive_pb2.WriteFileRequest(
                                fileHandle=file_handle,
                                startPos=offset,
                                length=len(data),
                                buffer=data,
                                closeFile=False,
                            ),
                        )
                    except Exception as e:
                        logger.error(
                            f"【Cd2Disk】上传失败 [WriteToFile]: {remote_path}, "
                            f"offset={offset}, chunk={len(data)}, {e}"
                        )
                        return None
                    bytes_written = int(getattr(write_resp, "bytesWritten", len(data)) or 0)
                    if bytes_written != len(data):
                        logger.error(
                            f"【Cd2Disk】上传失败，写入长度不匹配: {remote_path}, "
                            f"expect={len(data)}, actual={bytes_written}"
                        )
                        return None
                    offset += bytes_written

            try:
                close_resp = self._call_authed(
                    "CloseFile",
                    CloudDrive_pb2.CloseFileRequest(fileHandle=file_handle),
                )
            except Exception as e:
                logger.error(f"【Cd2Disk】上传失败 [CloseFile]: {remote_path}, {e}")
                return None
            if not self._is_success(close_resp):
                logger.error(f"【Cd2Disk】上传失败，关闭文件失败: {remote_path}")
                return None
            file_handle = 0

            # 等待 CD2 完成云端上传，避免源文件被过早删除
            self._wait_upload_complete(remote_path)

            return self.get_item(Path(remote_path))
        except Exception as e:
            logger.error(f"【Cd2Disk】上传失败: {local_path} -> {remote_path}, {e}")
            return None
        finally:
            if file_handle > 0:
                try:
                    self._call_authed("CloseFile", CloudDrive_pb2.CloseFileRequest(fileHandle=file_handle))
                except Exception:
                    pass

    def _wait_upload_complete(
        self, remote_path: str, timeout: int = 3600, interval: int = 5,
        stall_timeout: int = 600,
    ) -> bool:
        """
        等待 CD2 将文件上传到云端完成。
        通过轮询 GetUploadFileList 检查目标文件的上传状态。

        :param timeout: 总超时时间（秒），默认 3600（1 小时）
        :param interval: 轮询间隔（秒）
        :param stall_timeout: 无进度超时（秒），如果持续 stall_timeout 秒没有新字节传输则超时
        """
        # 终态集合 — 这些状态表示上传已结束
        terminal_statuses = {
            CloudDrive_pb2.UploadFileInfo.Finish,
            CloudDrive_pb2.UploadFileInfo.Error,
            CloudDrive_pb2.UploadFileInfo.FatalError,
            CloudDrive_pb2.UploadFileInfo.Cancelled,
            CloudDrive_pb2.UploadFileInfo.Skipped,
            CloudDrive_pb2.UploadFileInfo.Ignored,
        }

        normalized = self._normalize_path(remote_path)
        deadline = time.monotonic() + timeout
        found_ever = False
        last_transferred = -1
        last_progress_time = time.monotonic()

        while time.monotonic() < deadline:
            try:
                resp = self._call_authed(
                    "GetUploadFileList",
                    CloudDrive_pb2.GetUploadFileListRequest(getAll=True),
                )
                upload_files = getattr(resp, "uploadFiles", []) or []

                # 查找匹配的上传任务
                found = False
                for uf in upload_files:
                    dest = self._normalize_path(getattr(uf, "destPath", "") or "")
                    if dest == normalized or dest.rstrip("/") == normalized.rstrip("/"):
                        found = True
                        found_ever = True
                        status_enum = getattr(uf, "statusEnum", None)
                        status_str = getattr(uf, "status", "")
                        transferred = int(getattr(uf, "transferedBytes", 0) or 0)
                        total = int(getattr(uf, "size", 0) or 0)

                        if status_enum in terminal_statuses:
                            if status_enum == CloudDrive_pb2.UploadFileInfo.Finish:
                                logger.info(f"【Cd2Disk】云端上传完成: {remote_path}")
                            else:
                                logger.warning(
                                    f"【Cd2Disk】云端上传终态非成功: {remote_path}, "
                                    f"status={status_str}, enum={status_enum}"
                                )
                            return status_enum == CloudDrive_pb2.UploadFileInfo.Finish

                        # 检测上传进度，有新字节则刷新无进度超时
                        now = time.monotonic()
                        if transferred > last_transferred:
                            last_progress_time = now
                            last_transferred = transferred

                        # 无进度超时检测
                        stall_elapsed = now - last_progress_time
                        if stall_elapsed >= stall_timeout:
                            logger.warning(
                                f"【Cd2Disk】云端上传停滞超时({stall_timeout}s 无新进度): {remote_path}, "
                                f"transferred={self._human_size(transferred)}/{self._human_size(total)}"
                            )
                            return False

                        progress_pct = f"{transferred / total * 100:.1f}%" if total > 0 else "N/A"
                        logger.debug(
                            f"【Cd2Disk】等待云端上传: {remote_path}, status={status_str}, "
                            f"progress={progress_pct}, "
                            f"transferred={self._human_size(transferred)}/{self._human_size(total)}"
                        )
                        break

                if not found:
                    if found_ever:
                        # 之前出现过又消失了，认为已完成
                        logger.info(f"【Cd2Disk】上传任务已从队列消失，视为完成: {remote_path}")
                        return True
                    # CD2 可能还没来得及创建上传任务，或者已经秒传完成

            except Exception as e:
                logger.debug(f"【Cd2Disk】查询上传状态失败: {remote_path}, {e}")

            time.sleep(interval)

        elapsed = timeout
        logger.warning(f"【Cd2Disk】等待云端上传超时({elapsed}s): {remote_path}")
        return False

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        if size_bytes <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"]
        i = 0
        value = float(size_bytes)
        while value >= 1024 and i < len(units) - 1:
            value /= 1024
            i += 1
        return f"{value:.2f} {units[i]}"

    def is_support_transtype(self, transtype: str) -> bool:
        """
        判断是否支持指定的传输类型。
        供 MoviePilot 媒体整理时判断 storage_oper 能力。
        """
        return transtype in ("move", "copy")

    def usage(self) -> Optional[StorageUsage]:
        base_root = self._token_root or "/"
        logger.info(f"【Cd2Disk】开始空间统计, base_root={base_root}")

        # 遍历一级子目录，只统计真正的云盘根目录（isCloudRoot=true）
        # 自挂载的目录不是 isCloudRoot，会被自然过滤
        entries: list = []
        try:
            roots = self._list_cloud_files(base_root, force_refresh=False)
            logger.info(f"【Cd2Disk】根目录下共 {len(roots)} 个子条目")
            for one in roots:
                name = getattr(one, "name", "") or ""
                full_path = getattr(one, "fullPathName", "") or ""
                is_cloud_root = bool(getattr(one, "isCloudRoot", False))
                is_dir = bool(getattr(one, "isDirectory", False))
                cloud_api = getattr(one, "CloudAPI", None)
                cloud_name = getattr(cloud_api, "name", "") if cloud_api else ""

                # 只统计云盘根目录，跳过自挂载/普通目录
                if not is_cloud_root:
                    logger.debug(
                        f"【Cd2Disk】跳过非云盘根目录: name={name}, path={full_path}, "
                        f"isDir={is_dir}, isCloudRoot={is_cloud_root}, cloudName={cloud_name}"
                    )
                    continue

                if not full_path:
                    full_path = f"/{name}" if name else ""
                if not full_path:
                    continue

                normalized = self._normalize_dir_path(str(full_path))
                if normalized == "/":
                    continue

                try:
                    space = self._call_authed("GetSpaceInfo", CloudDrive_pb2.FileRequest(path=normalized))
                    t = int(getattr(space, "totalSpace", 0) or 0)
                    u = int(getattr(space, "usedSpace", 0) or 0)
                    f = int(getattr(space, "freeSpace", 0) or 0)
                    logger.info(
                        f"【Cd2Disk】云盘空间: name={name}, cloudName={cloud_name}, path={normalized}, "
                        f"total={self._human_size(t)}, used={self._human_size(u)}, free={self._human_size(f)}"
                    )
                    if t > 0 or u > 0 or f > 0:
                        entries.append((t, u, f, normalized))
                    else:
                        logger.debug(f"【Cd2Disk】跳过空间全为零的云盘: {normalized}")
                except grpc.RpcError as e:
                    code = e.code()
                    if code == grpc.StatusCode.UNAUTHENTICATED:
                        self._log_auth_error_once("获取空间信息", normalized, e)
                    elif code in (
                        grpc.StatusCode.PERMISSION_DENIED,
                        grpc.StatusCode.NOT_FOUND,
                        grpc.StatusCode.INVALID_ARGUMENT,
                    ):
                        logger.debug(f"【Cd2Disk】跳过无权限或无效路径的空间统计: {normalized}, {code}")
                    else:
                        logger.warning(f"【Cd2Disk】获取空间信息失败: {normalized}, {e}")
                except Exception as e:
                    logger.warning(f"【Cd2Disk】获取空间信息失败: {normalized}, {e}")
        except Exception as e:
            logger.debug(f"【Cd2Disk】枚举空间统计路径失败: {base_root}, {e}")

        if not entries:
            # 没有 isCloudRoot 的子目录时回退到根路径
            logger.info("【Cd2Disk】未找到 isCloudRoot 子目录，回退到根路径获取空间")
            try:
                space = self._call_authed("GetSpaceInfo", CloudDrive_pb2.FileRequest(path=base_root))
                total = int(getattr(space, "totalSpace", 0) or 0)
                free = int(getattr(space, "freeSpace", 0) or 0)
                used = int(getattr(space, "usedSpace", 0) or 0)
                logger.info(
                    f"【Cd2Disk】根路径空间: total={self._human_size(total)}, "
                    f"used={self._human_size(used)}, free={self._human_size(free)}"
                )
                if total > 0 or used > 0 or free > 0:
                    available = free if free > 0 else max(total - used, 0)
                    logger.info(
                        f"【Cd2Disk】空间统计结果(根路径回退): total={self._human_size(total)}, "
                        f"available={self._human_size(available)}"
                    )
                    return StorageUsage(total=total, available=available)
            except Exception as e:
                logger.warning(f"【Cd2Disk】根路径空间统计也失败: {e}")
            return None

        # 按 (total, used) 去重，避免同一存储空间被重复统计
        seen: set = set()
        unique: list = []
        for t, u, f, path in entries:
            key = (t, u)
            if key not in seen:
                seen.add(key)
                unique.append((t, u, f, path))
            else:
                logger.debug(
                    f"【Cd2Disk】去重跳过: path={path}, total={self._human_size(t)}, "
                    f"used={self._human_size(u)} (与已有条目重复)"
                )

        total = sum(t for t, _, _, _ in unique)
        used = sum(u for _, u, _, _ in unique)
        free = sum(f for _, _, f, _ in unique)

        if total <= 0 and used <= 0 and free <= 0:
            logger.warning("【Cd2Disk】所有云盘空间信息均为零")
            return None

        available = free if free > 0 else max(total - used, 0)
        logger.info(
            f"【Cd2Disk】空间统计结果: {len(unique)} 个云盘, "
            f"total={self._human_size(total)}, used={self._human_size(used)}, "
            f"free={self._human_size(free)}, available={self._human_size(available)}"
        )
        return StorageUsage(total=total, available=available)

    def close(self):
        try:
            if self._channel is not None:
                self._channel.close()
                self._channel = None
        except Exception:
            pass
