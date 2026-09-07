# Movie_Data_Capture 集成

媒藏 0.7.0 将用户提供的 `/Users/dengyintao/Desktop/workspace/Movie_Data_Capture/` 作为功能基准，内置其完整影片刮削引擎。导入基线为提交 `f7af27e` 及该工作区中尚未提交的 Linux、后台服务和核心处理修正。

## 集成范围

- 18 个成人影片 Provider：JavLibrary、JavDB、JavBus、AirAV、Fanza、XCity、Jav321、MGStage、FC2、AVSOX、DLsite、Carib、Madou、Getchu、Gcolle、JavDay、PissPlay、JavMenu
- 2 个普通影片 Provider：TMDB、IMDb
- 原始番号解析、Provider 优先级和指定 Provider 搜索
- MDC 模式 1（刮削并整理）、模式 2（仅整理）、模式 3（原目录刮削）
- NFO、封面、缩略图、剧照、预告片、演员头像、字幕、多段影片、翻译、繁简转换、封面裁剪及水印
- zero-operation 预览、离线处理、文件正则过滤、任务日志、取消和定时任务
- 页面映射源 `config.ini` 的全部 20 个配置分组；翻译密钥保存后不由 API 回传

## 运行方式

MDC 在独立子进程中运行，避免其全局配置和单例状态影响媒藏的索引及 qB 后台服务。每项任务会在应用私有数据目录生成独立 `config.ini`，执行前检查输入、成功输出和失败输出是否位于 fnOS 授权目录中。任务完成后，媒藏会重新索引对应媒体目录。

为保证飞牛离线安装可用，构建脚本将 CPython 3.12 Linux x86_64 所需的 requests、lxml、Pillow、OpenCC、cloudscraper、numpy、dlib 和人脸识别模型打进 FPK。由于包含本机架构动态库，0.7.0 的 manifest 平台为 `x86`；ARM 需要单独的依赖包和 FPK。

## 许可证

内置源码保留 Movie_Data_Capture 的 GNU GPL v3 许可证，许可证文件位于 `backend/third_party/movie_data_capture/LICENSE`。源代码未压缩或混淆，随媒藏仓库及 FPK 一同提供。
