# Colvins Source Service

独立于现有 OmniBox 的 v1 骨架项目。

当前目标：
- 独立 Docker 部署
- source 管理
- subscription 管理
- CatPaw/Open 与 TVBox 导出
- 后台管理入口
- 为后续 JS/Python source 执行内核预留数据模型

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

## 当前限制

- `/api/runtime/source/{id}` 仍是 stub
- 还未接入真正的 source 执行内核
- 还未接入网盘/直播/搜索/详情运行时
