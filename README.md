# rfs - Remote File Server

HTTP 文件服务器 + CLI/TUI 客户端，用于局域网内快速传输文件。

## 组件

- **tinyhttp.py** — 零依赖 HTTP 文件服务器（仅需 Python 3 标准库），支持上传、下载、目录浏览、软删除、mkdir、rename 等操作
- **rfs.py** — CLI 客户端，类似 scp 的语法进行文件传输
- **rfs_tui.py** — 基于 Textual 的交互式 TUI，支持并发传输、进度显示、MD5 校验

## 快速开始

### 服务端

```bash
# 在目标机器上启动（无外部依赖）
python3 tinyhttp.py --port 8580 --bind 0.0.0.0
```

### 客户端

```bash
# 安装
uv sync

# CLI 使用
rfs ls                         # 列出远程根目录
rfs ls docker/                 # 列出子目录
rfs cp local.txt :             # 上传到远程 /
rfs cp local.txt :/dir/        # 上传到指定目录
rfs cp :remote.txt .           # 下载到当前目录
rfs rm :/path/file.txt         # 软删除（移到 .Trash）
rfs mkdir :/new-dir            # 创建远程目录
rfs mv :/old :/new             # 重命名

# TUI 使用
rfs ui
```

## TUI 快捷键

| 键 | 功能 |
|---|---|
| 2xEnter | 进入目录 / 下载文件 |
| u | 上传（调用系统文件选择器） |
| x | 取消选中的传输任务 |
| d | 删除远程文件 |
| m | 创建目录 |
| r | 重命名 |
| . | 切换隐藏文件显示 |
| F5 | 刷新 |
| Backspace | 返回上级目录 |
| q | 退出 |

## 特性

- MD5 校验：上传/下载完成后自动比对本地与服务端 MD5
- 并发传输：多个上传/下载任务同时执行，互不阻塞
- 软删除：删除操作移至 .Trash 目录并记录元数据，支持回退
- 重名检测：上传时检测远程同名文件/目录，提供覆盖、重命名、取消选项
- 进度显示：实时速度、已传输/总大小、进度条
- 路径安全：服务端防止 `../` 路径穿越

## 配置

```bash
# 指定服务器地址和代理
rfs --server http://10.0.0.1:8580 --proxy http://proxy:7890 ls

# 禁用代理
rfs --no-proxy ls
```
