# mygwd 发布说明

mygwd 基于 [jacyl4/de_GWD](https://github.com/jacyl4/de_GWD)，继承
[XDcooler/de_GWD 的修复版本 667a39b](https://github.com/XDcooler/de_GWD/commit/667a39b3cdd069e4fad23e596540544875d8184d)。
原有作者、第三方组件及 EPL-2.0 许可证保留。

## 已包含的修复

- Debian 13、PHP 8.4 与校验过的官方 Pi-hole 6 安装资源。
- 客户端更新通过独立后台任务运行，持久记录状态，防止并发更新。
- 更新失败恢复 cron、系统解析器和正式 DNS 端口；成功前实际验证 UDP/TCP DNS。
- 部署面板后刷新 PHP 时间戳并重启 FPM，清除旧认证代码的 OPcache。
- Pi-hole 使用正式端口 53，V2DNS 使用 5350，避免更新后遗留引导端口。
- 在 sysctl.d 持久开启 IPv4 转发，代理服务启动时再次启用；DNS 访问开关不再关闭全局转发。
- 保留节点与管理密码，按服务身份设置配置文件读取权限。

## 仓库与安装入口

client、server 的资源下载，以及面板的官方更新入口统一指向 `XDcooler/mygwd` 的 `main` 分支。
客户端安装 ZIP 与源码同步生成。IP、域名及广告规则继续使用已声明的上游数据来源。
系统内既有目录、服务名和配置格式保持兼容，完整安装步骤见 [客户端部署指南](client-deployment.md)。

## 实机验证记录

2026-10-21，在 Debian 13.7 amd64 网关上完成基础修复版本 `667a39b` 的完整在线更新，
状态为 `succeeded`，退出码为 0。8 个关键服务恢复 active，UDP/TCP DNS 正常；
局域网电脑访问百度、Google generate_204、GitHub 分别返回 200、204、200。
节点、密码、网络地址及默认网关在更新前后保持不变，配置仅更新规则应用时间。

面板登录页 200、未登录主页 302、错误密码 401，管理 broker 能以 Web 服务身份读取配置；
本轮没有单独使用正确面板密码登录。命名及仓库地址迁移在上述基础修复版本之上完成，
不表示已将此次仓库地址变更再次部署到该网关。

隔离回归、真实网络验证的方式及历史边界见 [回归测试说明](../tests/README.md)、
[客户端更新与恢复](client-update-recovery.md) 和 [Debian 13 验证记录](debian13-validation.md)。
