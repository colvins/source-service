# Colvins Source Service

独立于现有 OmniBox 的 v1 骨架项目。

当前目标：
- 独立 Docker 部署
- source 管理
- subscription 管理
- CatPaw/Open 与 TVBox 导出
- 后台管理入口
- JS/Python source 执行内核
- 网盘账号管理、Quark 分享文件扫描与播放候选解析

## 启动

```bash
cd /opt/colvins-source-service
docker compose up -d --build
```

OmniBox bridge 默认关闭。仅开发排障时才建议显式打开：

```bash
OMNIBOX_API_URL=http://your-host:7023/api/spider/omnibox
```

同时将系统设置 `drive_bridge_mode` 改为 `omnibox-fallback`。正常交付路径应保持 `disabled`。

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
- Quark 后台扫码登录入口和 QR API
- Quark / UC / 百度 / 阿里 / 115 / 123 / 迅雷 / 天翼 provider 入口
- 通用分享链接基础解析接口
- Quark 分享目录递归扫描、本地视频文件筛选
- Quark 转存后 `/file/v2/play` 转码候选解析，并保留 RAW/原画直链候选优先
- Quark 嵌套分享目录转存映射缓存，避免重复转存同一个文件
- Quark 播放候选直链过滤、候选排序
- 明确阻止 localhost / 127.0.0.1 / `/proxy` 作为播放地址
- `OMNIBOX_API_URL` 仅保留为显式开发 fallback，不再默认调用

还未完成：

- Quark cookie 自动刷新
- Quark 转存 token 校验异常的兼容处理，需要完整移动端授权或进一步 API 适配
- UC / 百度 / 阿里 / 115 / 123 / 迅雷 / 天翼原生播放实现

## Quark API

```text
GET  /api/drive/providers
GET  /api/drive/quark/status
POST /api/drive/quark/auth/qr/start
POST /api/drive/quark/auth/qr/check
POST /api/drive/share/parse
POST /api/drive/{provider}/share/parse
POST /api/drive/{provider}/share/info
POST /api/drive/{provider}/share/files
POST /api/drive/{provider}/share/videos
POST /api/drive/{provider}/share/play
POST /api/drive/{provider}/share/play-best
POST /api/drive/quark/share/parse
POST /api/drive/quark/share/info
POST /api/drive/quark/share/files
POST /api/drive/quark/share/play
POST /api/drive/quark/normalize-play
```

网页测试入口：

```text
GET /drive-test?provider=quark&shareURL=...
```

## 当前限制

- 当前已关闭默认 OmniBox fallback；如 Quark 账号缺少可转存/播放授权，播放接口会返回明确错误而不是偷偷代理
- 网页 source 编辑器还未做成完整 Monaco/依赖管理体验
