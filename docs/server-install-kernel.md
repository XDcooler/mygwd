# 服务端 Bash 安装与内核更新

适用 Debian 13（trixie）amd64 / arm64。使用 root shell；应用安装要求 systemd、可用 APT 源及外网访问。
Debian 11/12 需先按发行版指南迁移系统，安装器不会替代跨版本升级。

## 下载与安装

```bash
apt-get update
apt-get install -y ca-certificates curl
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 120 \
  https://raw.githubusercontent.com/XDcooler/mygwd/main/server -o /root/mygwd-server
bash -n /root/mygwd-server
bash /root/mygwd-server --help
bash /root/mygwd-server --install example.com
```

将 `example.com` 替换为自己的域名，先将域名解析到 VPS，确保入站 TCP 80/443 可访问。
默认 443 入口通过 ACME HTTP 验证签发证书；DNS 指向错误、80 端口阻断或签发失败会使安装返回非零。
自定义端口写作 `example.com:8443`，会交互读取 Cloudflare API Key 和邮箱，通过 DNS 验证签发证书。
不带参数运行 `bash /root/mygwd-server` 仍打开菜单，菜单 1 为安装。

应用更新前，应重新执行上面的下载和语法检查，并确认成功；本地旧脚本不会自行升级。完整更新命令、固定提交方式及验收见 [客户端与服务端更新指南](update-guide.md)。

应用更新使用新下载的脚本：

```bash
bash /root/mygwd-server --update
```

安装/更新共用互斥锁，失败会报告阶段并恢复本次停止的 cron。依赖安装失败、代理核心 ZIP 损坏或缺失成员、
证书签发和配置生成失败都会中止后续阶段，不显示成功。核心包通过验证后才替换核心；启动前检查 Nginx
和 Xray 配置，并将 Xray 配置设为 root:www-data 0640。
该恢复机制不等于完整系统回滚；已有服务器更新前应保留配置、节点和证书备份。

应用安装准备自身依赖；系统整体升级和内核升级使用各自流程。

## 内核更新和 BBR

服务端交互菜单与客户端对齐：

| 菜单 | 行为 |
| --- | --- |
| 2. Enable Debian kernel BBR | 启用当前内核 BBR，不安装内核、不重启 |
| 3. Install XanMod LTS kernel, enable BBR and reboot (amd64) | 安装官方 XanMod LTS、配置新内核 BBR + fq，完成启动校验后自动重启 |

菜单 3 与客户端共用 `scripts/install-xanmod.py`，只支持 Debian 13 amd64 的标准 GRUB 实体机或虚拟机，拒绝 ARM64、容器和不兼容的 Secure Boot/UEFI 环境。原内核、源、GRUB 与 BBR 配置会先备份，校验失败不进入重启；已安装包不会自动卸载。详见 [两端 XanMod 安装与验收](client-xanmod.md)。应用安装、更新和规则刷新不会自动调用此操作。

以下命令继续负责 Debian 官方内核维护与单独启用 BBR；菜单 2 不再调用 Debian 内核安装：

```bash
# 按现有 APT 索引显示计划，不安装、不刷新索引。
bash /root/mygwd-server --kernel-check

# 刷新 APT 索引，安装匹配的内核元包，检查内核、模块与 initramfs。
bash /root/mygwd-server --update-kernel

# 在安排好的维护窗口手动重启后，核对运行内核和关键服务。
reboot
uname -r
systemctl is-active nginx vtrui doh-server pihole-FTL iptablesrules

# 独立开启当前内核的 BBR；这一步不安装内核。
bash /root/mygwd-server --enable-bbr
```

`--kernel-check` / `--update-kernel` 按运行内核匹配以下元包：

| 架构与类型 | 元包 |
|---|---|
| amd64 标准 | `linux-image-amd64` |
| amd64 cloud | `linux-image-cloud-amd64` |
| arm64 标准 | `linux-image-arm64` |
| arm64 cloud | `linux-image-cloud-arm64` |

使用现有 APT 配置中的候选版本，APT 继续执行仓库签名与包校验。cloud 类型保持 cloud，
不强制切换到其他内核系列。容器内安装被拒绝，需在宿主机更新；自定义、实时及 ARM64 16K 内核不由此入口迁移。
Debian `--update-kernel` 安装保留旧内核，不执行 purge/autoremove，不设置清理内核的开机任务，也不自动重启。菜单 3 的 XanMod 流程在校验后会自动请求重启，两者独立。
包状态记录位于 `/var/backups/mygwd-kernel.*`；新内核是否实际运行须在重启后用 `uname -r` 确认。

元包机制见 Debian 官方的 [cloud amd64 包](https://packages.debian.org/trixie/linux-image-cloud-amd64)、
[arm64 包](https://packages.debian.org/trixie/linux-image-arm64) 和 [内核升级说明](https://www.debian.org/releases/trixie/release-notes/upgrading.en.html#upgrading-your-kernel-and-related-packages)。

## 验证范围

`tests/test_server_installer.py` 对安装各阶段做故障注入，覆盖非法输入、EOF、核心 ZIP 验证及更新失败恢复 cron。
Linux 上还使用真实随包 Xray 校验生成配置，启动真实 Nginx，以临时测试证书验证 HTTPS 200 和管理路径 404。
`tests/test_kernel_update.py` 验证四种元包选择、只读计划、容器/权限拒绝、APT 失败、缺失启动文件、旧内核保留及状态记录。
`tests/test_xanmod_install.py` 通过临时文件与命令适配器验证两端菜单 2/3、服务端共享安装器加载、架构/容器拒绝、BBR、GRUB 与失败恢复，测试中不安装或重启内核。

GitHub 的 `Server Bash and Debian kernel checks` 使用一次性 Debian 13 容器运行这些检查，
并在容器自己的文件系统中实际安装 cloud 内核包、执行 Debian 包钩子并检查 initramfs。
该包安装测试仅在测试驱动里模拟 VM 平台，生产入口仍拒绝容器。
它不验证 VPS 的真实域名 ACME 签发，也不包含 GRUB/UEFI 引导、重启后网络和运行内核验证。
生产服务器没有因本次源码检查被升级内核或重启。
