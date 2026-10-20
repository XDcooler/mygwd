# 客户端更新与恢复

Web 面板“准备官方更新”下载固定的官方 client。打开更新控制台后自动启动后台更新，
跳过 Debian 整体升级；客户端依赖仍按安装流程安装。控制台显示私有更新日志。
关闭控制台、ttyd 退出、Nginx 连接中断或控制台超过 15 分钟限制都不会停止后台更新。
SSH 菜单仍保留是否升级 Debian 的选择，也可用 `sudo bash client --update` 直接更新客户端。

后台任务是独立的 `de-gwd-update.service`，不属于 `updateGWD.service` 的进程组。
更新脚本复制到 `/var/lib/de-gwd-update/client` 后执行，避免原下载脚本在清理时被删除。
旧版控制台在选择菜单 0 时也会切换到独立任务。
控制台的 `KillMode=control-group` 保留，用于完整清理控制台及其他交互式安装进程。

更新页每 5 秒读取受登录保护的状态接口。状态保存在
`/var/lib/de-gwd-update/status.json`，包含阶段、结果、退出码与时间；接口不返回原始日志或节点信息。
日志 `/var/lib/de-gwd-update/update.log`、worker 脚本和锁文件位于 root 私有目录。
Web 与 SSH 更新共享排他锁，更新运行中不允许重新下载覆盖脚本。

失败后检查面板所显示的阶段，或通过 SSH 检查：

```sh
sudo cat /var/lib/de-gwd-update/status.json
sudo tail -n 100 /var/lib/de-gwd-update/update.log
sudo journalctl -u de-gwd-update -u vtrui -u iptables-proxy --no-pager
```

修复原因后重新准备并运行官方更新。重试从头执行，重新验证下载；不跳过已执行阶段，
也不自动恢复历史版本。断电、SIGKILL 等无法捕获的中断会在下次查询时显示“已中断”；
这种情况下先用 `sudo systemctl restart cron` 恢复定时任务，再检查服务并重试。
普通失败及 HUP/INT/TERM 会记录失败阶段并尝试恢复已停止的 cron；cron 自身启动失败会单独报错。

切换到引导 DNS 端口前，更新器会在私有目录记录原 Pi-hole 端口和系统解析器（包括符号链接）。
更新失败时先恢复 cron，再恢复 Pi-hole 正式端口、原系统解析器，并启动 DNS 服务。
若 V2DNS 仍处于引导期的 53 端口，则先迁回 5350，避免与 Pi-hole 冲突。
DNS 必须实际应答 UDP 和 TCP 查询；服务 active 不代表 DNS 已恢复，必要时会重启 DNS 链再检查。
进入规则刷新前和更新结束前都要通过本地 53 端口的查询检查，才会记录成功。

面板 PHP 文件部署完成后统一更新时间戳、检查 FPM 配置并重启 PHP 8.4 FPM，
确保旧页面轮询期间缓存的认证代码失效。安装 ZIP 仍保留可复现时间戳，不再依赖压缩包时间
触发 OPcache 更新。SmartDNS 的停止信号改为 TERM，避免 STOP 仅挂起进程而阻塞重启。

更新流程先等待 Xray active 且 TCP 9896 监听，再启动代理规则，规则启动最多尝试 3 次。
配置重建后先启动 Xray、设置 UDP 模式、恢复代理，再刷新规则数据。
Nginx 配置先做 `nginx -t`，运行中的服务使用 reload，未运行时启动。

以 www-data 运行的 Xray 服务配置使用 `root:www-data 0640`，包括主实例、分流与转发实例；
主配置 `0conf`、备份及 root 服务配置保持 `root:root 0600`。
安装器、节点配置辅助脚本和面板 broker 都保持该权限规则。
PHP 将空参数序列化为 JSON 对象，broker 同时兼容旧页面发出的空数组。

2026-10-20 验证：更新故障注入与 broker 回归通过；在 Debian 13 ARM64 临时 fixture 中，
使用独立测试 systemd unit 启动 worker，停止其父控制台 unit 后 worker 完成，状态为 succeeded。
这验证进程生命周期分离，不表示已对实际节点运行完整在线更新。

本次回归结果：更新与 broker 14 项、透明代理 11 项、服务分流 9 项、Pi-hole 安装器 18 项、
规则数据 19 项、安装包与源码一致性 1 项，共 72 项通过。
规则数据测试在 macOS 使用独立 Git 运行时执行，其余在 Debian VM 执行；
102 个 PHP 文件语法检查通过。另通过真实 PHP 8.4 → broker 的配置读取检查，
以及以 www-data 身份读取服务配置、拒绝读取主配置/root 服务配置的权限检查。
没有在已部署网关上运行完整安装或更新，没有变更节点配置；结束时 vtrui、Nginx、cron 均为 active。

后续登录/联网修复验证：17 项更新及 broker 回归在 Debian 13 amd64 通过。
独立临时 FPM 池重现相同 1980 mtime 导致旧 opcode 存活，并验证部署刷新函数清空缓存。
现场发现 Pi-hole 遗留 1053、系统仍查询本地 53；恢复正式端口后，三轮百度/Google/GitHub
HTTPS 分别返回 200/204/200，LAN UDP/TCP DNS 正常，节点配置未变。

国内直连需要内核 IPv4 转发，即使代理链及 DNS 均正常，`net.ipv4.ip_forward=0` 也会使
LAN 设备访问国内站点超时。客户端现在将该设置写入 `/etc/sysctl.d/90-de-gwd-forwarding.conf`，
并在代理服务启动前再次启用转发。DNS 端口访问开关只维护对应防火墙规则，不再改动全局转发。
这也覆盖了没有 `/etc/sysctl.d/99-sysctl.conf` 兼容链接、开机不加载 `/etc/sysctl.conf` 的安装。
