from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import grpc

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

        parsed = urlsplit(cd2_url)
        scheme = parsed.scheme or "http"
        host = parsed.netloc or parsed.path
        if not host:
            host = "127.0.0.1:19798"
        self._origin_scheme = scheme
        self._origin_host = host

        self._channel = grpc.insecure_channel(host)
        self._stub = CloudDrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)
        token = (api_key or "").strip()
        if not token:
            raise RuntimeError("CloudDrive2 API key 不能为空")
        self._metadata: List[Tuple[str, str]] = [("authorization", f"Bearer {token}")]

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
        value = self._normalize_path(path)
        if value != "/":
            value = value.rstrip("/")
        return value

    def _normalize_file_path(self, path: str) -> str:
        value = self._normalize_path(path)
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
        result: List[Any] = []
        for reply in self._stub.GetSubFiles(req, metadata=self._metadata):
            for sub_file in reply.subFiles:
                result.append(sub_file)
        return result

    def _resolve_download_url(self, path: str) -> tuple[str, Dict[str, str]]:
        req = CloudDrive_pb2.GetDownloadUrlPathRequest(
            path=path,
            preview=False,
            lazy_read=False,
            get_direct_url=True,
        )
        info = self._stub.GetDownloadUrlPath(req, metadata=self._metadata)

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
        except Exception as e:
            logger.error(f"【Cd2Disk】递归遍历目录失败: {root}, {e}")
            return None

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        parent_path = self._normalize_dir_path(fileitem.path)
        target_path = self._join_path(parent_path, name)

        try:
            req = CloudDrive_pb2.CreateFolderRequest(parentPath=parent_path, folderName=name)
            resp = self._stub.CreateFolder(req, metadata=self._metadata)
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
            req = CloudDrive_pb2.FindFileByPathRequest(path=target_path)
            cloud_file = self._stub.FindFileByPath(req, metadata=self._metadata)
            if not getattr(cloud_file, "name", "") and not getattr(cloud_file, "fullPathName", ""):
                return None
            return self._to_file_item(cloud_file)
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

    def delete(self, fileitem: FileItem) -> bool:
        path = self._normalize_path(fileitem.path)
        try:
            resp = self._stub.DeleteFile(CloudDrive_pb2.FileRequest(path=path), metadata=self._metadata)
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
            resp = self._stub.RenameFile(req, metadata=self._metadata)
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
            resp = self._stub.MoveFile(req, metadata=self._metadata)
            if not self._is_success(resp):
                logger.error(
                    f"【Cd2Disk】移动失败: {src_path} -> {dst_dir}, {getattr(resp, 'errorMessage', '')}"
                )
                return False

            if target_name != src_name:
                result_paths = self._result_paths(resp)
                moved_path = result_paths[0] if result_paths else self._join_path(dst_dir, src_name)
                rename_resp = self._stub.RenameFile(
                    CloudDrive_pb2.RenameFileRequest(theFilePath=moved_path, newName=target_name),
                    metadata=self._metadata,
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
            resp = self._stub.CopyFile(req, metadata=self._metadata)
            if not self._is_success(resp):
                logger.error(
                    f"【Cd2Disk】复制失败: {src_path} -> {dst_dir}, {getattr(resp, 'errorMessage', '')}"
                )
                return False

            if target_name != src_name:
                result_paths = self._result_paths(resp)
                copied_path = result_paths[0] if result_paths else self._join_path(dst_dir, src_name)
                rename_resp = self._stub.RenameFile(
                    CloudDrive_pb2.RenameFileRequest(theFilePath=copied_path, newName=target_name),
                    metadata=self._metadata,
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

        existed = self.get_item(Path(remote_path))
        if existed and not self.delete(existed):
            logger.error(f"【Cd2Disk】上传失败，无法覆盖已有文件: {remote_path}")
            return None

        file_handle = 0
        try:
            create_resp = self._stub.CreateFile(
                CloudDrive_pb2.CreateFileRequest(parentPath=remote_dir, fileName=target_name),
                metadata=self._metadata,
            )
            file_handle = int(getattr(create_resp, "fileHandle", 0) or 0)
            if file_handle <= 0:
                logger.error(f"【Cd2Disk】上传失败，创建远端文件句柄失败: {remote_path}")
                return None

            offset = 0
            with open(local_path, "rb") as f:
                while True:
                    data = f.read(8 * 1024 * 1024)
                    if not data:
                        break
                    write_resp = self._stub.WriteToFile(
                        CloudDrive_pb2.WriteFileRequest(
                            fileHandle=file_handle,
                            startPos=offset,
                            length=len(data),
                            buffer=data,
                            closeFile=False,
                        ),
                        metadata=self._metadata,
                    )
                    bytes_written = int(getattr(write_resp, "bytesWritten", len(data)) or 0)
                    if bytes_written != len(data):
                        logger.error(
                            f"【Cd2Disk】上传失败，写入长度不匹配: {remote_path}, "
                            f"expect={len(data)}, actual={bytes_written}"
                        )
                        return None
                    offset += bytes_written

            close_resp = self._stub.CloseFile(
                CloudDrive_pb2.CloseFileRequest(fileHandle=file_handle),
                metadata=self._metadata,
            )
            if not self._is_success(close_resp):
                logger.error(f"【Cd2Disk】上传失败，关闭文件失败: {remote_path}")
                return None
            file_handle = 0

            return self.get_item(Path(remote_path))
        except Exception as e:
            logger.error(f"【Cd2Disk】上传失败: {local_path} -> {remote_path}, {e}")
            return None
        finally:
            if file_handle > 0:
                try:
                    self._stub.CloseFile(
                        CloudDrive_pb2.CloseFileRequest(fileHandle=file_handle),
                        metadata=self._metadata,
                    )
                except Exception:
                    pass

    def usage(self) -> Optional[StorageUsage]:
        total = 0
        used = 0
        targets: List[str] = []

        try:
            roots = self._list_cloud_files("/", force_refresh=False)
            for one in roots:
                full_path = getattr(one, "fullPathName", "")
                if not full_path:
                    name = getattr(one, "name", "")
                    full_path = f"/{name}" if name else ""
                if not full_path:
                    continue

                normalized = self._normalize_dir_path(str(full_path))
                if normalized != "/" and normalized not in targets:
                    targets.append(normalized)
        except Exception:
            pass

        if not targets:
            targets = ["/"]

        for target in targets:
            try:
                space = self._stub.GetSpaceInfo(
                    CloudDrive_pb2.FileRequest(path=target),
                    metadata=self._metadata,
                )
                total += int(getattr(space, "totalSpace", 0) or 0)
                used += int(getattr(space, "usedSpace", 0) or 0)
            except Exception as e:
                logger.warning(f"【Cd2Disk】获取空间信息失败: {target}, {e}")

        if total <= 0 and used <= 0:
            return None

        return StorageUsage(total=total, used=used)

    def close(self):
        try:
            self._channel.close()
        except Exception:
            pass
