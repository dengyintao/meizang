# GitHub Release 自动更新

服务器端更新器位于 `scripts/meizang-release-updater.sh`，面向 fnOS x86 版媒藏。

工作流程：

1. 从 `dengyintao/meizang` 获取最新稳定 Release。
2. 仅在版本高于当前安装版本时下载 `meizang.fpk`。
3. 校验 Release 中的 SHA-256，并检查 FPK 的应用 ID、版本和平台。
4. 使用飞牛官方 `appcenter-cli install-fpk` 完成升级。
5. 通过 Unix Socket 健康接口确认新版本启动成功。

服务器安装位置：

- 更新脚本：`/usr/local/sbin/meizang-release-updater`
- Cron 配置：`/etc/cron.d/meizang-release-updater`
- 更新日志：`/vol2/@appdata/meizang/release-updater.log`

计划任务每 30 分钟检查一次。手动检查可运行：

```bash
sudo /usr/local/sbin/meizang-release-updater
```

`--force` 可用于重新安装当前最新版，正常计划任务不会降级或重复安装：

```bash
sudo /usr/local/sbin/meizang-release-updater --force
```
