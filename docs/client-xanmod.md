# 客户端与服务端 XanMod 内核、BBR 与重启

客户端和服务端 SSH 菜单均提供以下两项功能：

- **2. Enable Debian kernel BBR**：启用当前内核支持的 BBR，不安装内核，不重启。
- **3. Install XanMod LTS kernel, enable BBR and reboot (amd64)**：安装官方 XanMod LTS 内核，配置新内核 BBR + fq，验证启动准备后自动请求整机重启生效。

两端共用同一安装器。只有显式选择菜单 3 才执行 XanMod 安装；客户端 `--update`、`--web-update`、面板应用更新、服务端 `--install` / `--update` 及规则刷新不会调用它。服务端 `--kernel-check` / `--update-kernel` 继续维护 Debian 官方内核，保留原先的手动重启方式。

## 平台和软件源

支持 Debian 13、amd64 的实体机或虚拟机，使用已有标准 GRUB 启动配置。ARM64 和容器拒绝执行。UEFI 环境中 Secure Boot 必须明确处于关闭状态，并通过 `efibootmgr` 确认当前启动项为 Debian GRUB；无法确认时拒绝。脚本不改变固件、安全启动密钥或引导器类型。

使用 [XanMod 官方 APT 软件源](https://xanmod.org/#apt_repository) 的 `trixie main`，通过 HTTPS 下载密钥与软件包索引，限定 `signed-by` 密钥环，并核对官方主密钥完整指纹：

```text
D38D7D1DA1349567ADED882D86F7D09EE734E623
```

安装 `linux-xanmod-lts-x64v1` 及其精确依赖的内核和头文件。这是官方提供的 LTS x86-64 兼容分支，不需要宿主 CPU 或虚拟机暴露 x86-64-v3 指令集。软件包版本从软件源读取，不硬编码某一内核小版本；不安装 Edge/RT 或旧 BBRplus 5.10。

密钥来源为官方链接 [`archive.key`](https://dl.xanmod.org/archive.key)。2026-10-25 已读取官方 trixie 软件包目录确认该元包存在；未来官方轮换密钥时需核对并更新指纹，脚本不会绕过校验。APT 依赖安装使用 `--no-remove`，保留旧内核；不会运行 purge 或 autoremove。

## 执行顺序

1. 校验 root、系统、架构、容器、GRUB、Secure Boot 和当前包配置状态；获取与 Debian 内核更新共用的操作锁。
2. 将运行内核、包记录、项目涉及的源/密钥、GRUB 和 BBR 配置备份到 root 私有的 `/var/backups/mygwd-xanmod.*`。
3. 准备 curl、CA、GnuPG、initramfs 依赖；校验密钥后注册带限定签名和包优先级的官方软件源。
4. 模拟安装，固定本次选定的元包版本，再执行实际安装；核对内核和头文件已配置完成、内核文件非空、模块依赖索引及 initramfs 可读取。
5. 从新内核的 `/boot/config-版本` 确认支持 BBR 与 fq，再写入下次启动的 BBR + fq 参数。若功能编译为模块，核对对应模块文件，并写入专用 `modules-load.d` 配置使其在启动应用参数前加载。清理 `/etc/sysctl.conf` 中对应的旧参数行，保留其他设置；此时不调整正在运行的旧内核。能力缺失、模块缺失或写入失败时停止重启。
6. 将项目 GRUB 片段设置为默认启动第 0 项并显示 5 秒菜单；生成并校验 GRUB，确认第 0 项的内核和 initramfs 就是新安装版本，再清理旧的下一次启动覆盖项。
7. 同步磁盘，调用 `systemctl reboot`。SSH 可能随重启断开；重启交接期间避免因 SSH 关闭信号将已验证的启动项恢复成旧设置。

默认第 0 项会由后续正常包更新继续生成；若自定义 GRUB 排序使新 XanMod 不是第 0 项，或其他配置覆盖本项目的默认项，脚本停止并恢复配置，不会靠“包安装成功”推断重启后一定使用新内核。

## 失败与重启后验收

下载安装、签名、包状态、initramfs 或 GRUB 校验失败时，不进入重启请求，尝试恢复本轮备份的启动、源和 BBR 文件，并报告错误。恢复失败会明确指出备份位置。APT 已安装的软件包和依赖仍保留，配置恢复不等于完整系统回滚。不能用脚本中的检查替代具体虚拟机、网卡与 DKMS 驱动的启动验证。

客户端重新连接后检查：

```bash
uname -r
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc
systemctl is-active nginx php8.4-fpm pihole-FTL smartdns v2dns vtrui iptables-proxy cron
dig @127.0.0.1 www.baidu.com A +time=2 +tries=1
dig +tcp @127.0.0.1 www.baidu.com A +time=2 +tries=1
```

服务端重新连接后检查：

```bash
uname -r
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc
nginx -t
systemctl is-active nginx vtrui doh-server pihole-FTL iptablesrules cron
dig @127.0.0.1 www.baidu.com A +time=2 +tries=1
dig +tcp @127.0.0.1 www.baidu.com A +time=2 +tries=1
```

两端 `uname -r` 均应包含 `xanmod` 并与各自备份目录的 `result.json` 目标版本一致，拥塞控制与默认队列应分别为 `bbr` 和 `fq`。安装完成时写入的是重启后的目标配置，只有重新连接后读取运行值才能确认实际生效。再从局域网验证国内网页、国外代理及节点切换；服务端还需由真实客户端验证节点与 DoH 连通。若新内核不能启动，可通过机器控制台选择 GRUB 中保留的旧内核；本脚本不提供自动硬件故障恢复。

设计中的 GRUB 默认项与下一次启动项语义以 [GNU GRUB 官方手册](https://www.gnu.org/software/grub/manual/grub/grub.html) 为依据。
