# 整理失败源文件清理插件 设计文档

**日期：** 2026-03-29
**状态：** 已批准

---

## 概述

新增 `TransferFailDelete` 插件。当 MoviePilot v2 媒体整理失败时，自动删除源文件，逻辑与现有「删除转移记录和源文件」操作保持一致。

---

## 架构

### 事件驱动

监听 `EventType.TransferFailed`（v2 专属，`"transfer.failed"`），无需轮询。

事件 payload 包含：
- `fileitem`：`schemas.FileItem`，源文件信息（含 `.path`、`.storage` 等）
- `transferinfo`：`TransferInfo`，`.message` 为自由文本失败原因
- `download_hash`：字符串，种子 hash（可能为空）

### 删除逻辑

与 `DELETE /api/v1/history/transfer?deletesrc=true` 保持一致：

```
StorageChain().delete_media_file(fileitem)
  → 调用对应存储后端删除文件（本地 / 网盘）
  → 递归清理空的父目录（不超过配置的下载根目录）

若 download_hash 非空：
  DownloadFiles.delete_by_fullpath(path)
  eventmanager.send_event(DownloadFileDeleted, {"src": path, "hash": hash})
```

---

## 插件信息

| 字段 | 值 |
|---|---|
| 类名 | `TransferFailDelete` |
| 目录 | `plugins.v2/transferfaildelete/` |
| `plugin_name` | 整理失败源文件清理 |
| `plugin_desc` | 媒体整理失败时自动删除源文件 |
| `plugin_config_prefix` | `transferfaildelete_` |
| `plugin_order` | 99 |
| `auth_level` | 2 |

---

## 配置项

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `False` | 是否启用插件 |
| `notify` | bool | `True` | 删除后是否发送通知消息 |

---

## 核心流程

```
on_transfer_failed(event):
  if not self._enabled: return
  fileitem = event.event_data.get('fileitem')
  if not fileitem: return
  transferinfo = event.event_data.get('transferinfo')
  download_hash = event.event_data.get('download_hash')

  # 删除文件
  result = StorageChain().delete_media_file(fileitem)

  # 清理下载记录
  if download_hash:
    DownloadFiles.delete_by_fullpath(db, Path(fileitem.path).as_posix())
    eventmanager.send_event(DownloadFileDeleted, {
      "src": fileitem.path,
      "hash": download_hash
    })

  # 通知
  if self._notify:
    self.post_message(
      mtype=NotificationType.Manual,
      title="整理失败源文件已删除",
      text=f"文件：{fileitem.path}\n原因：{transferinfo.message if transferinfo else '未知'}"
    )
```

---

## 明确不做的事

- 不删除整理失败历史记录（保留供用户查看）
- 不过滤失败原因（无枚举，自由文本过滤意义有限）
- 不支持撤销（与原有删除逻辑一致）
- 不兼容 v1（`TransferFailed` 事件为 v2 专属）

---

## 文件结构

```
plugins.v2/
└── transferfaildelete/
    └── __init__.py        # 插件主体

package.v2.json            # 新增 TransferFailDelete 条目
```

---

## 依赖

| 依赖 | 来源 | 用途 |
|---|---|---|
| `_PluginBase` | MoviePilot 核心 | 插件基类 |
| `StorageChain` | MoviePilot 核心 | 删除文件（含存储后端分发） |
| `DownloadFiles` | MoviePilot 核心 | 清理下载文件记录 |
| `EventType.TransferFailed` | MoviePilot v2 | 整理失败事件 |
| `EventType.DownloadFileDeleted` | MoviePilot 核心 | 通知下载器文件已删除 |
| `NotificationType` | MoviePilot 核心 | 发送通知 |
