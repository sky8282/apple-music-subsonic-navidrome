<img width="2872" height="2236" alt="1" src="https://github.com/user-attachments/assets/3e2874ed-7a7d-4315-a5f0-c1f0c6c22240" />

# 🍎 Apple Music to Subsonic/Navidrome Bridge
基于 FastAPI 构建的高性能桥接服务。它能够将 Apple Music 的海量高解析度曲库无缝接入到支持 Subsonic / Navidrome 协议的第三方播放器（如 Feishin / ds one / 箭头音乐 等）中。

✨ 功能特性
播放体验：模拟 Subsonic / Navidrome 接口协议，适配第三方音乐客户端。

实时流媒体解密：利用 downloader或者 go run main.go ，实现下载与本地解密流式转发。

元数据：采用 AMP-API + 网页脱壳爬虫 双引擎架构，抓取（US/CN区）歌手简介、封面（强行限制 300x300）及相关艺人。

歌词与播放列表：内置第三方歌词代理模糊匹配算法（未完善），支持 SQLite 驱动的云端自建歌单双向同步。

Token 管理：自动侦测并从苹果网页端抽取有效 Bearer Token，支持内存与硬盘双重缓存，过期/失效自动续期。

# 🛠️ 环境要求
1. Linux / macOS / Windows
2. Python 3.8+ (用于运行核心桥接服务)
3. Go 1.18+ (仅用于编译 Go 下载器核心，若已有对应架构的二进制文件则无需安装)
4. 必须具备苹果解密环境，如：MP4box 等



# 💻 📱 客户端推荐:
💻 电脑端：
```text
feishin 1.2.0 版本 （只适配这个版本的feishin）
```
📱 手机端：
```text
1. ds cloud （已改名 ds one）
2. 箭头音乐
```
# 🚀 部署与使用指南
### 📂 核心目录结构如下：
```text
/root/apple-bridge/
├── main.py                   # 桥接服务启动入口
├── subsonic.py               # Subsonic 核心路由
├── navidrome.py              # Navidrome 协议路由
├── apple_music_api.py        # 苹果 API 及爬虫核心
├── database.py               # 播放列表 SQLite 数据库
├── requirements.txt          # 项目依赖清单
├── user.txt                  # 用户鉴权账密配置，格式必须为: 账号:密码
├── apple_token_cache.json    # 自动生成Token 缓存文件
├── temp_cache/               # 自动生成音频临时存放目录，5分钟后自动删除以保持服务器硬盘空间
├── downloader                # 解压对应系统的 downloader-linux-x64.zip 或 downloader-mac-arm.zip
└── config.yaml               # 必须填入你的  media-user-token 
```
### 1. 安装 Python 依赖：
```text
pip install -r requirements.txt
```
### 2. 配置账号密码
user.txt 的文件，请按照 用户名:密码 的格式写入，用于拦截播放器的非法请求，格式如下:
```text
sky666:sky666
```
### 3.项目启动：
```text
python main.py
```

## 🧭 使用指南与注意事项
### 1. 目录读写权限
临时缓存 (./temp_cache)：程序会在运行目录下自动创建该文件夹用于存放下载的音频文件。请确保运行此脚本的用户具有对当前目录的读写权限，否则流媒体转发将报 500 错误。
Token 缓存 (apple_token_cache.json)：请勿手动锁定或修改该文件的只读权限。程序需要随时对其进行重写覆写以保持系统持续运转。

### 2. 风控与并发限制 (Anti-Ban Limits)
系统底层（subsonic.py）内置了严格的并发锁：DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)。
含义：同时触发的底层解密下载任务最多只有 2 个。这是为了防止由于客户端预加载或瞬间高频并发，导致苹果服务器判定恶意请求而封锁服务器 IP。强烈建议不要轻易调大此数值。

### 3. 虚拟模块与视频降级
苹果的部分专辑会附带 MV 或特供视频。由于 Subsonic 客户端主要针对音频设计，目前本桥接器对视频内容做了安全的降级占位处理。
如果您在部分歌手详情页中看到一个名为 ★ Videos ★ 的专辑，这是正常的虚拟占位符设计，用于拦截非法的视频解析请求，防止客户端崩溃，并非 Bug。

## ⚠️ 免责声明
本项目仅供学习、研究和代码技术交流使用。
严禁将本项目用于任何商业用途或大规模公开服务。使用本项目产生的一切后果及账号风险由使用者自行承担。

```mermaid
flowchart TD
    %% 核心节点定义
    Client["💻 📱 第三方播放器如:<br>💻: Feishin 1.2.0版本<br>📱: ds one<br>📱: 箭头音乐"]
    Bridge["🖥️ FastAPI 桥接服务<br>"]
    
    %% 鉴权判定中心
    Auth{{"🛡️ 校验 user.txt<br>账号密码 与 Bearer Token"}}
    Router{"🔀 路由分发"}
    401["⛔ 返回 401 Unauthorized<br>拒绝访问"]
    
    %% 元数据节点
    API["🍎 Apple Music API<br>(携带自动续期 Token)"]
    Spider["🕸️ 网页脱壳引擎<br>使用 CN / US 区域<br>抓取艺人介绍与相关艺人"]
    Format["🧩 解析并重组为<br>Subsonic / Navidrome 格式"]
    
    %% 流媒体节点
    Stream["🌊 流媒体处理中心<br>(进入 Semaphore 并发锁)"]
    Downloader["⚙️ Downloader<br>(二进制可执行文件)<br>或<br>(go run main.go)"]
    Temp["📁 写入 ./temp_cache 目录<br>(生成对应的 .m4a)"]
    Clean["🗑️ 5分钟后自动销毁文件<br>保护服务器硬盘存储"]

    %% --- 鉴权分支 ---
    Client -->|"(Subsonic / Navidrome)<br>请求"| Bridge
    Bridge -->|"1. 拦截请求"| Auth
    Auth -->|"❌ 失败"| 401
    Auth -->|"✅ 成功"| Router

    %% --- 元数据分支 ---
    Router -->|"2. 元数据请求<br>(专辑/歌手/相关艺人)"| API
    API -->|"❌ 触发国区阉割 / 404"| Spider
    API -->|"✅ 正常返回"| Format
    Spider --> Format
    Format --> Client

    %% --- 音频播放分支 ---
    Router -->|"3. 播放请求<br>(/rest/stream)"| Stream
    Stream -->|"挂起当前 Python 线程<br>唤起外部进程"| Downloader
    Downloader -->|"下载 + 解密"| Temp
    Temp -->|"▶️ FileResponse<br>(支持 Accept-Ranges)"| Client

    %% --- 清理机制 ---
    Temp -.->|"后台异步任务<br>(BackgroundTasks)"| Clean

    %% ==========================================
    %% 样式定义
    %% ==========================================
    style Client fill:#1c1c1e,stroke:#007AFF,color:#fff
    style Bridge fill:#2c2c2c,stroke:#fff,color:#fff
    
    style Auth fill:#FF9F0A,stroke:#fff,color:#000
    style Router fill:#1c1c1e,stroke:#007AFF,color:#fff
    style 401 fill:#8B0000,stroke:#FF3B30,color:#fff
    
    style API fill:#1c1c1e,stroke:#007AFF,color:#fff
    style Spider fill:#3a3a3c,stroke:#FF3B30,color:#fff
    style Format fill:#007AFF,stroke:#007AFF,color:#fff
    
    style Stream fill:#1c1c1e,stroke:#007AFF,color:#fff
    style Downloader fill:#004d00,stroke:#30D158,color:#fff
    style Temp fill:#1c1c1e,stroke:#30D158,color:#fff
    style Clean fill:#3a3a3c,stroke:#FF9F0A,color:#fff
