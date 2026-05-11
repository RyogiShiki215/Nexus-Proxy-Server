<div align="center">

# 🔮 Nexus Proxy Server

**轻量级 AI 提示词优化中转代理**

对下游暴露 OpenAI 兼容 API · 纯 Python 标准库 · 零第三方依赖

[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-GPLv3-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](#docker-部署推荐)

</div>

## ✨ 特性一览

| 类别 | 功能 |
|:----:|------|
| 🔌 | **OpenAI 兼容 API** — 下游直接用 OpenAI SDK 接入，支持流式/非流式 |
| 🎭 | **身份池轮换** — deviceId + Proxy + UA + 伪造 IP 原子绑定，自动轮换 |
| 🔄 | **两种策略** — `random`（随机分散）/ `exhaust`（用完再换） |
| ⚡ | **429 自动重试** — 遇到限速自动切换身份，最多重试 3 次 |
| 🖥️ | **Web 管理面板** — 身份池、API Key、设定、Playground 一站式管理 |
| 🔐 | **安全加固** — 密码哈希、随机密码、IP 限速、认证失败封禁 |
| 🔑 | **Key 精细管控** — 独立 RPM、用量上限、有效期 |
| 💉 | **API 前置注入** — 可全局注入 system prompt 到所有 API 请求 |
| 🕐 | **时区设定** — 使用量图表按所选时区对齐日期边界 |
| 📊 | **使用量统计** — 7 天柱形图，按 Key 分组，tooltip 明细 |
| 🤖 | **自动注册 & 清理** — 身份不足自动补充，耗尽身份定时清理 |
| 🐳 | **Docker 一键部署** — Dockerfile + docker-compose.yml |

---

## 🚀 快速启动

### Docker 部署（推荐）

```bash
docker compose up -d
```

查看启动密码：

```bash
docker logs nexus-proxy 2>&1 | head -20
```

### 本地运行

```bash
python server.py
```

> 无需 `pip install`，纯标准库，Python 3.8+ 即可运行。

启动后访问管理面板：**http://localhost:9800**

> ⚠️ 每次启动自动生成随机管理密码，仅在终端日志中显示一次。

---

## 📖 使用流程

### 1️⃣ 登录管理面板

打开 `http://localhost:9800`，输入终端显示的随机密码登录。

### 2️⃣ 添加身份

进入「身份池」页面：

| 字段 | 说明 |
|------|------|
| DeviceId | 留空自动生成 |
| 代理 | `http://user:pass@ip:port`（建议配置） |
| 伪造 IP | 留空自动随机生成公网 IP |
| UA | 留空从 1000 个 UA 中随机选取 |
| 每小时上限 | 该身份每小时最多调用次数（建议 3-5） |

支持批量添加，配合代理列表一次性导入。

### 3️⃣ 创建 API Key

进入「API 密钥」页面，可设定：

| 参数 | 说明 |
|------|------|
| RPM | 每分钟请求上限，0 = 无限 |
| 次数上限 | 最大使用次数，0 = 无限 |
| 有效期 | 支持 1 天 / 1 周 / 1 月 / 永久 |

### 4️⃣ 下游接入

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-wc-你的密钥",
    base_url="http://你的服务器:9800/v1"
)

response = client.chat.completions.create(
    model="wc-optimizer",
    messages=[{"role": "user", "content": "一个女孩在花园里"}]
)

print(response.choices[0].message.content)
```

### 5️⃣ Playground

管理面板「直接使用」页面可直接输入提示词优化，无需 API Key。支持前置注入越狱提示。

---

## ⚙️ 系统设定

### 轮换模式

| 模式 | 说明 |
|------|------|
| `random` | 每次随机选一个可用身份，分散风控压力 |
| `exhaust` | 优先用完一个身份的额度再换，最大化利用率 |

### 时区设定

在「系统设定 → 时区设定」中选择你所在的时区，影响仪表盘 7 天使用量图表的日期边界对齐。支持 20 个常用时区：

> UTC · 伦敦 · 巴黎 · 柏林 · 莫斯科 · 迪拜 · 印度 · 曼谷 · 中国大陆 · 香港 · 台北 · 新加坡 · 东京 · 首尔 · 悉尼 · 奥克兰 · 纽约 · 芝加哥 · 丹佛 · 洛杉矶

### API 前置注入

启用后，所有通过 `/v1/chat/completions` 的请求会在 messages 最前面插入一条 system 消息。可用于全局设定 AI 行为。

### 自动注册 & 清理

- **自动清理**：定时删除本小时已耗尽的身份
- **自动注册**：身份池数量低于目标时自动补充新身份

---

## 🔐 安全设计

| 机制 | 说明 |
|------|------|
| 密码安全 | SHA-256 哈希存储，每次启动生成新随机密码 |
| IP 限速 | 30 次/分钟，超限封禁 5 分钟 |
| 认证防护 | 连续 5 次失败封禁 10 分钟 |
| 请求体限制 | 可配置最大请求体大小（默认 512KB） |
| RPM 限制 | 每个 Key 独立设定，或使用全局默认值 |

---

## 🐳 Docker 配置

```yaml
# docker-compose.yml
services:
  nexus-proxy:
    build: .
    container_name: nexus-proxy
    restart: unless-stopped
    ports:
      - "9800:9800"
    volumes:
      - ./data:/app/data    # 持久化数据
    environment:
      - TZ=Asia/Shanghai
```

数据持久化在 `./data/` 目录，包含身份池、API Key、设定、使用量日志。

---

## 📁 文件结构

```
Nexus Proxy Server/
├── server.py            # 后端（纯标准库，零依赖）
├── user-agents.txt      # 1000 个 UA 列表
├── Dockerfile           # Docker 镜像定义
├── docker-compose.yml   # Docker Compose 配置
├── static/
│   ├── index.html       # 管理面板
│   ├── style.css        # 样式
│   └── app.js           # 前端逻辑
├── data/                # 运行时数据（自动创建）
│   ├── identity_pool.json
│   ├── api_keys.json
│   ├── settings.json
│   └── usage_log.json
└── README.md
```

---

## 📡 API 端点

### OpenAI 兼容

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | 聊天补全（支持 stream） |
| `GET` | `/v1/models` | 模型列表 |

### 管理接口

> 所有管理接口需要 `X-Admin-Password` 请求头

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/admin/status` | 系统状态概览 |
| `GET` | `/api/admin/identities` | 身份池列表 |
| `POST` | `/api/admin/identities` | 添加单个身份 |
| `POST` | `/api/admin/identities/batch` | 批量添加身份 |
| `DELETE` | `/api/admin/identities/:id` | 删除指定身份 |
| `POST` | `/api/admin/identities/remove-exhausted` | 删除已耗尽身份 |
| `POST` | `/api/admin/identities/remove-all` | 清空身份池 |
| `GET` | `/api/admin/keys` | 密钥列表 |
| `POST` | `/api/admin/keys` | 创建密钥 |
| `POST` | `/api/admin/keys/:key/revoke` | 撤销密钥 |
| `DELETE` | `/api/admin/keys/:key` | 删除密钥 |
| `GET` | `/api/admin/settings` | 获取设定 |
| `POST` | `/api/admin/settings` | 更新设定 |
| `GET` | `/api/admin/usage-stats` | 使用量统计数据 |
| `POST` | `/api/direct-optimize` | 直接优化（Playground） |

---

## 🧠 工作原理

本项目利用了某 Photoshop 插件内置的「提示词优化」功能的设计缺陷：

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────┐
│  下游调用方  │ ──→ │ Nexus Proxy  │ ──→ │  上游代理服务器  │ ──→ │  大模型   │
└─────────────┘     └──────────────┘     └─────────────────┘     └──────────┘
                         │
                    伪造身份：
                    · deviceId
                    · User-Agent
                    · X-Forwarded-For
                    · 签名 (SHA-256)
```

**关键发现：**

1. 🆓 **免费代呼叫** — 不需要用户 API Key，成本由插件作者承担
2. 🔓 **签名密钥硬编码** — 客户端 JS 明文包含共享密钥
3. 📐 **签名算法公开** — `sha256(timestamp + body + secret)`
4. 🎭 **身份可伪造** — `deviceId` 本地随机生成，非服务端签发
5. ⏱️ **限速仅靠 deviceId** — 换一个就是新额度

**等效于**：用 N 个虚拟设备身份，每个每小时免费调用若干次大模型，聚合成一个高可用 API 服务。

---

## ⚠️ 注意事项

- 代理质量决定稳定性，建议使用住宅代理或高匿代理
- 每小时上限建议 3-5，不要太激进
- 上游超时较长（最多 5 分钟），这是正常的
- 如果上游改了签名密钥，在设定页面同步修改
- 建议部署在境外 VPS，避免直连暴露真实 IP

---

<div align="center">

**本项目仅供学习研究，请勿用于商业用途或大规模滥用。**

</div>

---

## 📦 发布包与许可证

- 此目录为脱敏后的源码发布包
- 不包含真实运行数据；`data/` 下仅保留 `*.example.json` 模板
- Docker 使用通用环境变量配置：参考 `.env.example`
- 本项目采用 GNU General Public License v3.0，详见 `LICENSE`
- 签署人：Ha.
