# 媒藏

媒藏是面向飞牛 fnOS 的本地多媒体管理工具。它通过 Web 页面索引 NAS 上的图片、视频和音乐，查看技术元数据并识别完全重复文件；原始媒体始终留在用户自己的目录中。

当前版本为 `0.9.2`：

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
- MusicBrainz 音乐匹配、标签写入、专辑封面和按艺术家／专辑整理
- 可取消的音乐后台任务与逐文件失败原因

qB 整理暂只处理影片和字幕/NFO，不会移动音乐文件。

Movie_Data_Capture 引擎来自用户提供的本地 GPL-3.0 源码版本，完整源码和许可证随 FPK 分发；媒藏通过隔离的后台进程调用该引擎，并在执行前检查 fnOS 授权路径。当前 FPK 内置 CPython 3.12 Linux x86_64 运行依赖，因此本版本的 fnOS 平台标记为 `x86`。

qB 整理说明见 [做种兼容整理](docs/QBITTORRENT.md)。
Movie_Data_Capture 的功能边界、运行方式与许可证说明见 [MDC 集成说明](docs/MOVIE_DATA_CAPTURE.md)。
服务器自动跟随 GitHub Release 升级的说明见 [自动更新](docs/AUTO_UPDATE.md)。

## 本地运行

不需要安装第三方 Python 包：

```bash
chmod +x scripts/run-dev.sh
./scripts/run-dev.sh
```

打开 <http://127.0.0.1:8787/app/meizang>。

如果系统安装了 `ffprobe`，扫描音视频时会自动提取技术元数据；没有安装时仍可正常建立文件索引。

## 登录与访问控制

**通过飞牛桌面进入**：统一网关已校验 NAS 会话，应用直接信任网关注入的
`X-Trim-Username` / `X-Trim-Isadmin` 身份头，NAS 账户免登录即用
（NAS 管理员即应用管理员）。注意这些身份头仅在 Unix Socket 网关模式下采信，
直连 TCP 端口时会被忽略，防止伪造。

**直接访问 Web 端口**：首次打开会自动跳转到登录页引导创建管理员账户
（用户名 1~32 字符，密码至少 8 位，scrypt 哈希存储）。登录后签发 7 天有效期的
HttpOnly 会话 Cookie（服务端只存哈希），连续 8 次登录失败会锁定 IP 10 分钟。

**API Token**（可选，供脚本调用）：设置 `MEIZANG_TOKEN`（或启动参数 `--token`）
后，请求携带 `Authorization: Bearer <token>` 可直接调用 `/api/*`，
与会话登录并存。`/api/health` 健康检查始终免认证。

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
