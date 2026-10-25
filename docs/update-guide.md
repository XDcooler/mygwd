# 客户端与服务端更新指南

适用于已安装本项目或兼容 de_GWD 配置的 Debian 13（trixie）amd64 / arm64 机器。所有命令在目标机器的 root Bash 中执行；普通管理员先运行 `sudo -i`。客户端和服务端分别在各自机器操作，不要把两组更新命令在同一台机器依次运行。

## 更新功能与范围

| 操作 | 客户端 | 服务端 |
| --- | --- | --- |
| 项目应用更新 | 更新面板、管理脚本、代理资源、DNS、Nginx、Pi-hole 配置及服务 | 更新代理资源、DoH、Nginx、Pi-hole 配置、防火墙及服务 |
| 读取现有设置 | 从 `0conf` 读取节点、管理密码和面板设置，重建运行配置 | 从现有 Nginx/Xray 配置读取域名、端口、UUID、路径及上游设置，重建运行配置 |
| 独立后台更新 | `--web-update`；独立 systemd unit，关闭控制台后继续 | 没有对应后台参数；`--update` 前台执行，可在 screen/tmux 中运行 |
| 前台应用更新 | `--update`；跳过菜单中的 Debian 整体升级选项 | `--update`；准备依赖，不执行 Debian 整体升级 |
| 规则刷新 | `bash /opt/de_GWD/ui-updateRules` | 同左 |
| 内核更新 | 没有 `client --update-kernel` 入口，按 Debian 系统维护流程处理 | `--kernel-check` 查看计划，`--update-kernel` 安装匹配元包 |
| BBR | 菜单 2/3 启用当前内核 BBR | `--enable-bbr`；不安装内核 |

项目更新会重新生成管理配置并重启相关服务，可能短暂中断 DNS、代理和面板访问。已有标准节点和管理设置会被读取使用，但手工修改的 Nginx、DNS、服务文件不能假定会完整保留。操作前保存配置和证书，优先创建 VPS/NAS 虚拟机快照；应用更新失败恢复不等于完整系统回滚。

“最新项目版本”指 GitHub `XDcooler/mygwd` 的 `main` 分支。运行旧磁盘脚本不会自动将该脚本升级；应先重新下载。随包代理核心、固定 Pi-hole 和其他组件按项目指定版本部署，不代表安装各上游最新版本。每日 04:00 的规则刷新也不更新项目代码。

## 通过 Bash 更新最新项目

先确认 `cat /etc/os-release` 显示 Debian 13，`dpkg --print-architecture` 为 amd64 或 arm64，并能通过 HTTPS 访问 GitHub。已有安装应具备 curl、CA 证书、Python 3、jq 等依赖；若缺少 curl，在网络正常时先运行 `apt-get update && apt-get install -y ca-certificates curl`。

下面将入口保存到专用目录，避免与旧源码目录中的 `resource/` 混用。每组下载、语法检查、替换与执行用 `&&` 连接；下载失败时不会执行磁盘上的旧入口。不要删除连接符后逐行忽略错误执行。

### 服务端

```bash
install -d -m0700 /root/mygwd-updates
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 120 \
  https://raw.githubusercontent.com/XDcooler/mygwd/main/server \
  -o /root/mygwd-updates/server.download &&
bash -n /root/mygwd-updates/server.download &&
mv -f /root/mygwd-updates/server.download /root/mygwd-updates/server &&
bash /root/mygwd-updates/server --update
```

命令结束后立即运行 `echo $?` 查看退出码；0 表示脚本成功返回。成功结束会显示 `Update de_GWD [done]`，旧服务名和该显示名称是兼容设置。服务端尚没有客户端式 `status.json` 或独立后台更新服务，应保留终端输出；需要断开 SSH 时，先进入 screen/tmux 会话再执行。

下载的 `server` 不带参数时进入菜单，菜单 0 也是应用更新。`--install` 用于安装，不要用它代替已有服务器的 `--update`。

### 客户端（推荐后台模式）

```bash
install -d -m0700 /root/mygwd-updates
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 120 \
  https://raw.githubusercontent.com/XDcooler/mygwd/main/client \
  -o /root/mygwd-updates/client.download &&
bash -n /root/mygwd-updates/client.download &&
mv -f /root/mygwd-updates/client.download /root/mygwd-updates/client &&
bash /root/mygwd-updates/client --web-update
```

`--web-update` 也可从 SSH 使用：它将脚本复制到 root 私有目录，启动独立的 `de-gwd-update.service`，随后跟随日志显示。Ctrl+C 只退出日志查看，任务继续。日志跟随命令不会在更新完成后自动退出，也不能用它的退出码代表后台更新结果。

另开 SSH 会话查看：

```bash
cat /var/lib/de-gwd-update/status.json
tail -n 100 /var/lib/de-gwd-update/update.log
journalctl -u de-gwd-update --no-pager -n 100
```

以状态文件中的 `status` 为准：`running` 表示进行中，`succeeded` 且 `exit_code` 为 0 表示完成，`failed` 表示失败并带有 `phase`。首次启动时状态文件可能尚未生成，或短暂保留上一次状态，应结合本轮日志、`updated_at` 和服务状态判断。日志可能包含节点信息，仅在可信管理终端查看。

需要保持 SSH 在线、以前台退出码判断结果时，下载后改用：

```bash
bash /root/mygwd-updates/client --update
```

前台模式也记录 `status.json`，但不会自动把终端输出写入后台模式的 `update.log`。两种模式共享更新锁，重复启动不会并行部署。客户端不带参数进入菜单后，菜单 0 也可更新，并额外询问是否升级 Debian；上述两个明确参数均跳过这一整体升级选项，应用依赖仍可能通过 APT 安装或更新。

Web 面板的“准备官方更新 → 打开更新控制台”使用同样的后台流程。旧 de_GWD 面板可能仍指向原仓库；要切换到 mygwd，应先使用上面的 mygwd 下载地址更新一次，再核对面板更新来源。

新版管理入口识别 `XDcooler/de_GWD` 与 `jacyl4/de_GWD` 的已知旧版 `main/client` 标准更新命令，将保存在配置中的旧字段规范为 mygwd。读取面板时显示新入口，正常保存设置时持久化，避免旧元数据误阻断 DoH 等无关设置。此迁移不执行更新命令；浏览器提交的自定义命令和未知地址仍被拒绝。

完整客户端安装或更新在部署管理脚本后，会在同一管理锁下将 `0conf` 的更新命令写为官方 mygwd 入口，无需等待下一次保存 DoH。此步骤只改更新元数据，不执行旧命令，不改节点、DNS、密码或原有备份。命令行指定资源仓库或固定提交时，面板后续更新仍使用官方 `XDcooler/mygwd/main/client`。

两端安装器还会将遗留的 `DE_GWD_RULES_REPOSITORY=XDcooler/de_GWD` 或 `jacyl4/de_GWD` 自动改用 `XDcooler/mygwd`，忽略大小写并显示迁移提示。资源分支或提交参数继续保留；如果原来固定的是旧库专属提交，需改为 mygwd 中存在的提交，否则下载会失败。其余显式指定的 fork 不变。`DE_GWD_*` 环境变量、`/opt/de_GWD` 路径、服务名和旧备份文件名仍是兼容接口；来源署名和用于识别旧配置的字符串也继续保留。

## 固定提交版本与完整源码更新

默认远程脚本和资源从 `main` 获取。若更新期间分支被继续推进，多次请求可能处于不同提交；需要可复现部署时，应在 GitHub [提交记录](https://github.com/XDcooler/mygwd/commits/main/) 中选定完整的 40 位提交 SHA，将脚本下载 URL 中的 `main` 和资源分支同时固定为该 SHA。

以下示例中的 SHA 必须替换；只选择本机角色对应的一行：

```bash
# 已将所选提交的 server/client 下载并通过 bash -n 检查之后：
DE_GWD_RULES_BRANCH=完整的40位提交SHA bash /root/mygwd-updates/server --update
DE_GWD_RULES_BRANCH=完整的40位提交SHA bash /root/mygwd-updates/client --update
```

客户端固定提交时使用前台 `--update`，并保持 SSH 或 screen/tmux 会话；后台 worker 启动时不会保留这个临时环境变量。固定项目提交只固定项目资产，规则刷新仍可获取声明的上游最新规则数据。

从完整 Git checkout 更新时，先确认没有需要保留的本地修改，在该 checkout 中运行 `git pull --ff-only`，再以 root 运行本地 `bash server --update` 或 `bash client --update`。不要把新脚本放在旧 checkout 内直接运行：安装器会优先读取脚本同目录的本地资源，可能混入旧资源。

## 规则、安全补丁和内核分别更新

只更新规则，两端均运行：

```bash
bash /opt/de_GWD/ui-updateRules
```

规则包括 GeoIP、GeoSite、中国 IPv4 路由、广告源及已选服务分流，校验或应用失败会报告错误并尝试保留/恢复原配置。详细来源和边界见 [规则更新说明](rule-updates.md)。

Debian 安全补丁通过 APT 管理，项目更新不是完整安全更新。客户端 PHP 8.4 的定向更新示例：

```bash
apt-get -o APT::Update::Error-Mode=any update &&
apt-get install --only-upgrade php8.4-fpm php8.4-cli php8.4-common php8.4-gd &&
/usr/sbin/php-fpm8.4 -t &&
systemctl restart php8.4-fpm
dpkg-query -W php8.4-fpm php8.4-cli php8.4-common
```

按 Debian 安全公告核对完整包版本，版本号较低不一定代表缺少发行版回补的补丁。标准 mygwd 服务端不依赖 PHP，Pi-hole 6 也不使用 PHP，不要为此在服务端额外安装 PHP。若自行部署了其他 PHP 应用，应按其实际软件包、扩展和运行进程维护。

服务端内核更新使用已重新下载的 server：

```bash
bash /root/mygwd-updates/server --kernel-check
bash /root/mygwd-updates/server --update-kernel
```

保留旧内核，不自动重启；在维护窗口手动重启后核对实际运行内核。BBR 是另一项独立操作：`bash /root/mygwd-updates/server --enable-bbr`。架构、容器及自定义内核限制见 [服务端 Bash 安装与内核更新](server-install-kernel.md)。

## 更新后验收与失败处理

客户端：

```bash
systemctl is-active nginx php8.4-fpm pihole-FTL smartdns v2dns vtrui iptables-proxy cron
dig @127.0.0.1 www.baidu.com A +time=2 +tries=1
dig +tcp @127.0.0.1 www.baidu.com A +time=2 +tries=1
sysctl net.ipv4.ip_forward
```

还应从实际局域网设备检查面板登录、国内网页、国外代理和 UDP。服务 active 或本机能打开网页，不能单独证明局域网转发正常。IPv6 透明代理仍未实现完整接管，不属于此次更新文档的新增功能。

服务端：

```bash
nginx -t
systemctl is-active nginx vtrui doh-server pihole-FTL iptablesrules cron
dig @127.0.0.1 www.baidu.com A +time=2 +tries=1
journalctl -u nginx -u vtrui -u doh-server --no-pager -n 100
```

再从真实客户端检查 TLS、DoH 和代理连接；HTTPS 首页能打开不代表代理节点能用。

客户端普通失败会记录阶段并尝试恢复 cron、DNS 端口和系统解析器；服务端会报告失败阶段并尝试恢复本次停止的 cron。二者均不保证完整配置回滚。先修复失败原因，再重新下载并重试，不要用安装命令覆盖已有部署；必要时恢复事前快照。客户端更多恢复说明见 [客户端更新与恢复](client-update-recovery.md)。

Debian 11/12 不支持直接运行这些应用更新入口完成系统迁移。先备份，并按 Debian 11 → 12 → 13 的顺序迁移，或新建 Debian 13 虚拟机后迁移配置。
