# 139Strm

移动云盘（139 / 和彩云）专用的 STRM 生成器 + 302 直链服务，**原生支持 `.cas` 秒传文件播放**。

**为什么会有这个项目**：SmartStrm 是闭源项目，官方驱动列表里没有移动云盘，只能通过 OpenList 转接。
如果你的 OpenList 部署在境外（比如 Hugging Face Spaces），播放时所有流量都要绕一圈境外中转，卡到没法看。

本工具直连移动云盘官方接口，不经过任何中转：

```
Emby 播放器 ──► 139Strm (/d/xxx) ──302──► 移动云盘 CDN
                     │
                只做一次跳转
               不中转视频流
```

---

## 核心能力：`.cas` 秒传文件也能播放

移动云盘里的 `.cas` 文件通常只有几百字节——它是真实文件的"特征码"，不是视频本身。
播放器打开它只会拿到一段 JSON，根本放不了。

139Strm 会在**播放的那一刻**把它还原成真文件：

```
播放器请求 /d/xxx?cas=电影.mkv.cas
    │
    ├─ 1. 下载 264 字节的 .cas，解出 SHA256 / 大小 / 原始文件名
    ├─ 2. 调 /file/create 做秒传 → 云端"凭空"还原出一个 4.24GB 的真文件（约 0.7 秒）
    ├─ 3. 取这个临时文件的真实直链，302 给播放器
    └─ 4. 延迟 5 分钟自动彻底删除临时文件，云盘里不留痕迹
```

**不占你的云盘空间**（秒传是引用，不是复制），**不消耗上传流量**，**拖动进度条不会重复还原**（直链已缓存）。

---

## 实测结果

以下均用真实移动云盘账号实测通过：

| 能力 | 状态 | 实测结果 |
|---|---|---|
| Authorization 登录 | ✅ | 正常解析账号与有效期 |
| 路由策略查询 | ✅ | 自动取得 `personal-kd-njs.yun.139.com` 接入点 |
| 目录浏览 | ✅ | 根目录 7 个文件夹正常列出 |
| 递归扫描生成 STRM | ✅ | 93 个文件，0 错误 |
| 302 直链播放 | ✅ | 302 跳到 cmecloud.cn CDN，支持 Range 分段 |
| 直链缓存 | ✅ | 二次请求 1.3s → 0.17s |
| **CAS 秒传还原** | ✅ | 3 个文件均还原成功，0.65~1.6 秒 |
| **CAS 播放验证** | ✅ | HTTP 206 + `video/x-matroska` + MKV 魔数 `1a45dfa3` |
| **临时文件自动清理** | ✅ | TTL 到期后临时目录归零 |

还原样例（4.24GB 的 MKV）：

```
HTTP/2 206
Content-Type:   video/x-matroska
Content-Range:  bytes 0-1023/4554461676
前 16 字节:     1a45dfa3 a3428681 0142f781 0142f281   ← 标准 Matroska 文件头
```

协议层（签名算法、两层 AES 加解密）已与 OpenList 官方 Go 实现做过**逐字节对照验证**，12 项测试全部一致。

---

## 快速开始

### 方式一：Docker（推荐，支持 AMD64 / ARM64）

```bash
mkdir -p 139strm/{config,strm} && cd 139strm

cat > docker-compose.yml <<'EOF'
services:
  139strm:
    image: tianjian518/139strm:1.0
    container_name: 139strm
    restart: unless-stopped
    ports:
      - "8025:8025"
    volumes:
      - ./config:/app/config
      - ./strm:/strm
EOF

docker compose up -d
```

访问 `http://你的IP:8025`

### 方式二：直接运行

```bash
pip install -r requirements.txt
python app.py          # 默认端口 8025
```

### 方式三：自己构建

```bash
docker build -t 139strm .
docker run -d --name 139strm --restart unless-stopped \
  -p 8025:8025 \
  -v ./config:/app/config \
  -v ./strm:/strm \
  139strm
```

> **关键点**：`./strm` 这个目录必须同时挂载给 Emby / Jellyfin，两边路径保持一致。

---

## 三步配置

### 第 1 步：获取 Authorization

1. 浏览器开**无痕窗口**，登录 <https://yun.139.com/>
2. 按 `F12` 打开开发者工具 → 切到 **网络（Network）**
3. 在网页里随便点开一个文件夹，触发一个请求
4. 点开任一请求 → 右侧 **请求标头（Request Headers）** → 找到 `Authorization`
5. 它的值形如 `Basic xxxxxxxx...` —— **复制时把开头的 `Basic ` 和空格去掉**，只留后面那串 Base64

> ⚠️ 这是最容易踩的坑。官方实现里有硬性校验，带了 `Basic` 前缀会直接报
> `authorization should not include Basic prefix`。

**免抓包的替代方案**：登录 <https://mail.10086.cn/> 后复制完整 Cookie，粘贴到界面的"139 邮箱 Cookie"栏（需包含 `Os_SSo_Sid` 和 `RMKEY` 两项），配合手机号可免密登录。

### 第 2 步：保存并测试

界面填入 Authorization → 点**测试连接**。看到账号、根目录条目数、凭据有效期即表示成功。

### 第 3 步：生成 STRM

- 设置输出目录（容器内 `/strm`）
- 设置**访问地址**（如 `http://192.168.1.10:8025`）— 这会被写进每个 strm 文件，必须是播放器能连上的地址
- 点**浏览云盘目录**选好要生成的目录
- 点**开始生成**

---

## 接入 Emby / Jellyfin

1. 把 `./strm` 目录挂载给 Emby 容器（例如同样是 `/strm`）
2. Emby 后台 → 添加媒体库 → 类型选"电影"或"电视剧" → 路径选 `/strm`
3. 扫描即可入库（因为 strm 只有几十字节，入库非常快）
4. 播放时 Emby 请求 strm 里的地址，本项目 302 跳到移动云盘直链

### 生成的文件长什么样

| 云盘里的文件 | 生成的本地文件 | strm 内容 |
|---|---|---|
| `VID_20220807.mp4` | `VID_20220807.mp4.strm` | `http://192.168.1.10:8025/d/Fk9se2Ip...` |
| `电影.mp4.cas` | `电影.mp4.strm` | 同上 + `?cas=%E7%94%B5%E5%BD%B1.mp4.cas`（自动去掉 `.cas` 尾巴） |

`?cas=` 这个参数是关键——它告诉服务端"这个文件需要秒传还原"，普通文件不带这个参数。

---

## 配置说明

配置项保存在 `config/config.json`，也可通过环境变量注入（优先级更高）：

| 环境变量 | 说明 | 默认值 |
|---|---|---|
| `PORT` | 服务端口 | `8025` |
| `CONFIG_PATH` | 配置文件路径 | `/app/config/config.json` |
| `YUN139_AUTHORIZATION` | 移动云盘凭据 | 空 |
| `YUN139_CLOUD_TYPE` | 云类型 | `personal_new` |
| `YUN139_OUTPUT_DIR` | 默认输出目录 | `/strm` |

### CAS 相关配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `cas_enabled` | 是否对 `.cas` 做秒传还原播放 | `true` |
| `cas_temp_ttl` | 临时文件保留秒数，到点自动彻底删除 | `300` |
| `cas_allow_all_ext` | `false`=只还原视频；`true`=任何后缀都还原 | `false` |
| `cas_temp_dir_id` | 临时目录 ID（自动记录，勿手动改） | 空 |

> 临时文件放在个人云根目录的 `139STRM_TEMP` 文件夹里。
> 万一程序异常退出留下残留，界面上有「立即清空残留临时文件」按钮，或调用 `POST /api/cas/purge`。

### 云类型

| 值 | 说明 |
|---|---|
| `personal_new` | **新版个人云（默认，也是唯一支持 CAS 播放的类型）** |
| `personal` | 旧版个人云 |
| `family` | 家庭云 |
| `group` | 群组云（需填群组 ID） |

---

## 已知限制

### 1. 临时文件会短暂占用文件列表

播放 `.cas` 时，云盘根目录的 `139STRM_TEMP` 文件夹里会短暂出现一个还原出来的文件（默认 5 分钟后自动删除）。
这是秒传还原的必要代价——OpenList 的 CAS 实现也是同样的做法。
目录本身只有这一个，且被程序记住 ID，不会重复创建。

### 2. 秒传还原依赖云端源数据还存在

如果原始文件在移动云盘服务器上已被彻底清理，秒传会失败并提示
`秒传失败：云端已不存在该文件的源数据，.cas 已失效无法还原`。这种情况下没有任何办法恢复。

### 3. 直链有有效期

移动云盘直链带时效签名（实测约 15 分钟）。本项目缓存 2 小时，过期会重新获取。
Emby 每次播放都会重新请求，不受影响。

### 4. 凭据有效期

Authorization 约 1~2 个月过期，过期后重新抓包即可。程序会在剩余不足 15 天时尝试自动刷新，
刷新失败会提示重新获取。

### 5. 只有新版个人云支持 CAS 播放

`cloud_type` 必须是 `personal_new`。家庭云 / 群组云 / 旧版个人云走的是另一套接口，未实现秒传还原。

---

## 项目结构

```
139Strm/
├── app.py                  Flask 服务：管理界面、API、302 直链端点
├── yun139/
│   ├── crypto.py           协议层：calSign 签名、AES-CBC/ECB、Go 风格序列化
│   ├── client.py           API 客户端：登录、路由、文件列表、直链、秒传
│   ├── cas.py              CAS 解析与秒传还原（本节目的核心）
│   └── strm.py             STRM 生成器：递归扫描、增量生成、字幕下载
├── templates/index.html    管理界面（无外部依赖，可离线）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### API 一览

| 接口 | 说明 |
|---|---|
| `GET /` | 管理界面 |
| `GET/POST /api/config` | 读写配置 |
| `POST /api/test` | 测试连接 |
| `GET /api/list?folder=xx` | 浏览目录 |
| `POST /api/strm` | 启动生成任务 |
| `GET /api/strm/status` | 查询任务进度 |
| `GET /d/<file_id>` | **302 跳转（普通文件）** |
| `GET /d/<file_id>?cas=xxx.cas` | **302 跳转（CAS 秒传还原后播放）** |
| `GET /api/link/<file_id>` | 调试用：查看直链不跳转 |
| `GET /api/cas/status` | CAS 开关状态与待清理数量 |
| `POST /api/cas/purge` | 立即清空残留临时文件 |
| `GET /health` | 健康检查 |

---

## 和 SmartStrm 的关系

| | SmartStrm | 139Strm |
|---|---|---|
| 源码 | 闭源，不可修改 | 开源可读可改 |
| 移动云盘 | 不支持，需 OpenList 转接 | **原生直连** |
| `.cas` 秒传播放 | 需配合 OpenList-CAS | **原生内置** |
| 支持网盘 | 夸克/115/123/天翼/光鸭等 | 仅移动云盘 |
| 302 直链 | Pro 付费功能 | **免费内置** |
| 定时任务 | 支持 | 暂不支持（手动触发 / 调用 API） |
| 部署位置 | 需与网盘网络通畅 | 任意位置，只要播放器能访问 |

如果你只需要移动云盘，本项目更轻、更快、不依赖境外中转。
如果你需要管理多个网盘，SmartStrm + 境内 OpenList 转接仍是更好的选择。

---

## 许可与免责

- 本项目仅供个人学习研究使用，请勿用于商业或非法用途。
- 所处理的数据均来源于第三方网盘，开发者不对内容的合法性负责。
- 协议实现参考了 OpenList（AGPL-3.0）的 139 驱动与社区 CAS 分支，在此致谢。
