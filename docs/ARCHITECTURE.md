# 媒藏初版架构

## 产品边界

媒藏管理 NAS 上已经存在的真实文件，不把文件导入私有存储。初版只进行读取、索引和重复报告，不包含自动删除和未经预览的文件移动。

## 数据流

```text
fnOS 授权目录
    ↓
Scanner（类型、大小、mtime）
    ↓
Metadata Provider
    ├── Filename Provider
    ├── ffprobe Provider
    ├── NFO Provider
    └── TMDB Provider（可选）
    ↓
SQLite / WAL
    ↓
REST API
    ↓
响应式 Web UI
```

扫描器通过大小和纳秒级修改时间判断文件是否变化，仅对新增或变化文件重新计算 SHA-256。完整遍历成功后才删除已消失文件的索引；任何目录权限错误都会保留旧记录，防止 NAS 挂载掉线导致误清理。

## 与 Movie Data Capture 的衔接

当前 Provider 版本复用了其两个成熟思想：

1. 从文件名清理分辨率、编码、发布组等噪声并提取标题/年份。
2. 把影片元数据来源抽象为 Provider，并按 Filename → ffprobe → NFO → TMDB 的顺序合并结果；靠后的 Provider 可补全或覆盖通用字段。

TMDB 已作为首个网络 Provider 接入，使用用户自己的 API Token。没有直接复制 Movie Data Capture 的站点爬虫与文件搬运逻辑；后续站点适配仍通过独立、可测试的 Provider 接入，避免 Web 服务与特定站点耦合。

网络 Provider 可使用媒藏自己的 HTTP/HTTPS 代理配置。启用后由 Provider 显式创建代理连接，不修改也不依赖 fnOS 的系统代理；代理 URL 和其中的可选凭据保存在应用私有 SQLite 数据库，设置接口只返回启用与配置状态。

## 下一阶段

- 后台任务持久化及扫描进度
- 图片 EXIF 与缩略图缓存
- 音乐 TagLib/Mutagen 标签
- IMDb 与 Movie Data Capture 兼容 Provider
- 路径模板规则、Plan/Apply 与操作撤销
- 逻辑 Collection、标签和关联文件模型
