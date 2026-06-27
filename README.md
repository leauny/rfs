# rfs - Remote File Server

HTTP 文件服务器 + CLI/TUI 客户端，用于依赖受限环境的文件传输。

## 组件

- **tinyhttp.py** — 零依赖 HTTP 文件服务器（仅需 Python 3 标准库），支持上传、下载、目录浏览、软删除、mkdir、rename 等操作
- **rfs.py** — CLI 客户端，类似 scp 的语法进行文件传输
- **rfs_tui.py** — 基于 Textual 的交互式 TUI，支持并发传输、进度显示、MD5 校验

## 快速开始

### 一键部署

```bash
# curl
curl -sL https://raw.githubusercontent.com/leauny/rfs/main/tinyhttp.py | python3 - --port 8580 --bind 0.0.0.0

# wget
wget -qO- https://raw.githubusercontent.com/leauny/rfs/main/tinyhttp.py | python3 - --port 8580 --bind 0.0.0.0
```

### 服务端

```bash
# 下载后本地启动
curl -sO https://raw.githubusercontent.com/leauny/rfs/main/tinyhttp.py
python3 tinyhttp.py --port 8580 --bind 0.0.0.0

# 如果通过 nginx 子路径反代到 /rfs/
python3 tinyhttp.py --port 8580 --bind 127.0.0.1 --url-prefix /rfs
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

- 流式传输：服务端和 CLI/TUI 客户端均以恒定 ~30 MB 内存上传/下载任意大小的文件，不受文件体积限制
- 浏览器原生上传：默认页面支持拖拽多文件、实时速度/ETA 进度条、上传完成后自动刷新
- MD5 校验：上传/下载边写边算，避免额外读盘；本地与服务端 MD5 自动比对
- 并发传输：多个上传/下载任务同时执行，互不阻塞
- 软删除：删除操作移至 .Trash 目录并记录元数据，支持回退
- 重名检测：上传时检测远程同名文件/目录，提供覆盖、重命名、取消选项
- 进度显示：滑动窗口实时速度、已传输/总大小、ETA 剩余时间（d/h/m/s 动态格式）
- Keep-alive ping：浏览器上传期间每 30s 发送心跳，防止 nginx 等反向代理 502 超时
- 路径安全：服务端防止 `../` 路径穿越
- Python 3.6+：服务端不依赖任何第三方库，3.6 即可运行

## 配置

```bash
# 指定服务器地址和代理
rfs --server http://10.0.0.1:8580 --proxy http://proxy:7890 ls

# 通过 nginx 子路径反代访问
rfs --server https://example.com/rfs ls

# 禁用代理
rfs --no-proxy ls
```

### nginx 反向代理

根路径独立域名：

```nginx
server {
    listen 443 ssl;
    server_name rfs.example.com;

    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:8580;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

子路径 `/rfs/`：

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location = /rfs {
        return 301 /rfs/;
    }

    location /rfs/ {
        proxy_pass http://127.0.0.1:8580;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

子路径模式下，后端需要带同样的前缀启动：

```bash
python3 tinyhttp.py --bind 127.0.0.1 --port 8580 --url-prefix /rfs
```

## 测试

测试套件位于 `tests/`，使用 pytest。每个测试都会启动一个真实的
`tinyhttp.py` 子进程在临时目录里跑，覆盖：上传/下载完整链路、
流式 multipart 解析、`X-Content-MD5` 校验、JSON API（ls / mkdir /
rename / restore / stat / trash）、目录列表 HTML、路径穿越防御，以及客户端/服务端在大文件上传时的内存上界。

```bash
# 安装 dev 依赖（pytest）
uv sync --dev

# 运行
uv run pytest                  # 全量
uv run pytest -v               # 详细输出
uv run pytest tests/test_tinyhttp_upload.py::test_full_byte_spectrum_with_fake_boundary
```

