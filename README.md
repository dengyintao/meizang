# 媒藏

媒藏是面向飞牛 fnOS 的本地多媒体管理工具。它通过 Web 页面索引 NAS 上的图片、视频和音乐，查看技术元数据并识别完全重复文件；原始媒体始终留在用户自己的目录中。

当前版本为 `0.8.9`：

- 响应式 Web 管理页面，而非 CLI 产品
- SQLite 持久化媒体库和 WAL 模式
- 图片、视频、音频分类与增量扫描
- 扫描采用短事务批量落库，扫描期间仍可添加目录和保存设置
- NAS 掉线/权限异常保护
- SHA-256 完全重复检测与一键安全清理（每组保留一份并保护 qB 管理文件）
- 使用 ffprobe 读取可用的视频时长、编码和分辨率
- 可组合的 Filename、ffprobe、NFO 与 TMDB 元数据 Provider
- TMDB Provider 配置、中文元数据、海报展示与视频强制重刮
- 媒藏独立 HTTP/HTTPS 代理配置与 TMDB 连通性测试
- 完整内置 Movie_Data_Capture 引擎及 20 个影片 Provider
- MDC 全量 config.ini 页面配置、任务日志、取消、预览与定时执行
- NFO、封面/剧照、演员头像、字幕、翻译、水印和三种整理模式
- fnOS 统一网关、专用低权限用户和授权目录约束
- qB 指定分类的已完成影片无复制归档：媒体库保存真实文件，下载位置保留软链接
- qB 任务移除后连续确认再清理已登记软链接，保留真实媒体文件

音乐目前仅支持分类、索引和 ffprobe 技术信息；尚无在线音乐刮削、专辑封面、标签写入或歌手／专辑整理。qB 整理暂只处理影片和字幕/NFO。

Movie_Data_Capture 引擎来自用户提供的本地 GPL-3.0 源码版本，完整源码和许可证随 FPK 分发；媒藏通过隔离的后台进程调用该引擎，并在执行前检查 fnOS 授权路径。当前 FPK 内置 CPython 3.12 Linux x86_64 运行依赖，因此本版本的 fnOS 平台标记为 `x86`。

qB 整理说明见 [做种兼容整理](docs/QBITTORRENT.md)。
Movie_Data_Capture 的功能边界、运行方式与许可证说明见 [MDC 集成说明](docs/MOVIE_DATA_CAPTURE.md)。

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
