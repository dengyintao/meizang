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
    └── ffprobe Provider
    ↓
SQLite / WAL
    ↓
REST API
    ↓
响应式 Web UI
```

扫描器通过大小和纳秒级修改时间判断文件是否变化，仅对新增或变化文件重新计算 SHA-256。完整遍历成功后才删除已消失文件的索引；任何目录权限错误都会保留旧记录，防止 NAS 挂载掉线导致误清理。

## 与 Movie Data Capture 的衔接

初版复用了其两个成熟思想：

1. 从文件名清理分辨率、编码、发布组等噪声并提取标题/年份。
2. 把影片元数据来源抽象为 Provider，技术信息来自 ffprobe，后续可接 TMDB/IMDb Provider。

没有直接复制 Movie Data Capture 的站点爬虫与文件搬运逻辑。后续应将通用电影刮削整理成独立、可测试的 Provider，避免 Web 服务与特定站点耦合。

## 下一阶段

- 后台任务持久化及扫描进度
- 图片 EXIF 与缩略图缓存
- 音乐 TagLib/Mutagen 标签
- TMDB/IMDb 视频元数据 Provider
- 路径模板规则、Plan/Apply 与操作撤销
- 逻辑 Collection、标签和关联文件模型
