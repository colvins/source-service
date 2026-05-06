# Colvins Source Service

独立于现有 OmniBox 的 v1 骨架项目。

当前目标：
- 独立 Docker 部署
- source 管理
- subscription 管理
- CatPaw/Open 与 TVBox 导出
- 后台管理入口
- JS/Python source 执行内核
- 网盘账号管理与 Quark 候选直链筛选

## 启动

```bash
cd /opt/colvins-source-service
docker compose up -d --build
```

如需临时桥接已有 OmniBox 网盘能力，可在 `.env` 中设置：

```bash
OMNIBOX_API_URL=http://your-host:7023/api/spider/omnibox
```

## 默认端口

- `8788`

## 网页入口

- `/`：后台首页
- `/#sources`：源执行内核
- `/#subscriptions`：订阅导出
- `/#drive-accounts`：网盘账号
- `/#runtime-debug`：运行时调试
- `/#settings`：系统设置

## 订阅地址

CatPaw/Open：

```text
http://your-host:8788/api/subscription/{subscriptionId}/catvod/index.js.md5
```

TVBox：

```text
http://your-host:8788/api/subscription/{subscriptionId}/tvbox?token={subscriptionToken}
```

## 网盘能力现状

当前版本已经提供：

- 网盘账号保存与状态检查接口
- Quark / UC / 百度 / 阿里 / 115 / 123 / 迅雷 / 天翼 provider 入口
- 通用分享链接基础解析接口
- Quark 播放候选解析、直链过滤、候选排序
- 明确阻止 localhost / 127.0.0.1 / `/proxy` 作为播放地址
- 继续保留 `OMNIBOX_API_URL` 作为临时 fallback bridge

还未完成：

- Quark 二维码登录
- Quark cookie 自动刷新
- Quark 分享链接保存/转存/文件树递归
- 完整替换 OmniBox 网盘后端

## Quark API

```text
GET  /api/drive/providers
POST /api/drive/share/parse
POST /api/drive/{provider}/share/parse
POST /api/drive/{provider}/share/info
POST /api/drive/{provider}/share/files
POST /api/drive/{provider}/share/play
POST /api/drive/quark/share/parse
POST /api/drive/quark/share/info
POST /api/drive/quark/share/files
POST /api/drive/quark/share/play
POST /api/drive/quark/normalize-play
```

## 当前限制

- 网盘最终 URL 获取仍可临时桥接 OmniBox
- 网页 source 编辑器还未做成完整 Monaco/依赖管理体验
