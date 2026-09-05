# 媒藏

媒藏是面向飞牛 fnOS 的本地多媒体管理工具。它通过 Web 页面索引 NAS 上的图片、视频和音乐，查看技术元数据并识别完全重复文件；原始媒体始终留在用户自己的目录中。

当前版本为 `0.5.0` Provider 初版：

- 响应式 Web 管理页面，而非 CLI 产品
- SQLite 持久化媒体库和 WAL 模式
- 图片、视频、音频分类与增量扫描
- NAS 掉线/权限异常保护
- SHA-256 完全重复检测（只展示，不自动删除）
- 使用 ffprobe 读取可用的视频时长、编码和分辨率
- 可组合的 Filename、ffprobe、NFO 与 TMDB 元数据 Provider
- TMDB Provider 配置、中文元数据、海报展示与视频强制重刮
- fnOS 统一网关、专用低权限用户和授权目录约束

## 本地运行

不需要安装第三方 Python 包：

```bash
chmod +x scripts/run-dev.sh
./scripts/run-dev.sh
```

打开 <http://127.0.0.1:8787/app/meizang>。

如果系统安装了 `ffprobe`，扫描音视频时会自动提取技术元数据；没有安装时仍可正常建立文件索引。

## 构建飞牛 FPK

安装飞牛官方 `fnpack 1.2.3` 后：

```bash
chmod +x scripts/build-fpk.sh
./scripts/build-fpk.sh
```

只检查打包目录，不运行 fnpack：

```bash
./scripts/build-fpk.sh --stage-only
```

安装后，管理员可直接从媒藏页面选择并授权媒体目录。

详细设计见 [架构说明](docs/ARCHITECTURE.md) 和 [FPK 打包说明](docs/FNOS_PACKAGING.md)。
