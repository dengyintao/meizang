# 飞牛 fnOS 打包说明

当前打包结构遵循 fnOS Native 应用要求：

```text
manifest
ICON.PNG / ICON_256.PNG
app/
  server/
  ui/config
  ui/images/
cmd/main
config/privilege
config/resource
wizard/
```

关键设计：

- `platform=all`：代码是纯 Python，不包含架构相关二进制。
- `install_dep_apps=python312`：使用飞牛提供的 Python 3.12 运行时。
- `run-as=package`：服务以 `meizang` 专用用户运行。
- `disable_authorization_path=false`：在应用设置中显示目录授权。
- 使用 `/app/meizang` 统一网关和 `meizang.sock` Unix Socket，不暴露额外 TCP 端口。
- 数据库、PID 和日志保存于 `TRIM_PKGVAR`，应用升级不会覆盖数据库。
- 媒体目录必须属于 `TRIM_DATA_ACCESSIBLE_PATHS`，后端还会再次校验路径边界。

构建使用 `fnpack build --directory <path>`。安装测试可在 fnOS 上使用应用中心手动安装，或执行：

```bash
appcenter-cli install-fpk meizang.fpk
```

实机发布前必须验证安装、启动、停止、重启、升级、保留数据卸载、目录拒绝和目录掉线场景。
