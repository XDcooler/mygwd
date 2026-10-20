# de_GWD 客户端部署指南（Debian 13 / 威联通 NAS 虚拟机）

适用于当前仓库的 Debian 13（trixie）客户端，支持 amd64、arm64 和 systemd。本文以 QTS 5.2.x 的 Virtualization Station 虚拟机为例，普通 Debian 13 主机可从第 3 节开始。

部署顺序：**先确认 server 正常并取得真实节点 → 安装 client → 验证服务及节点 → 接入局域网设备**。所有域名、UUID 和路径示例均为占位内容，必须替换成 server 当前配置。本文不包含任何部署密码。

## 1. 规划地址和资源

以下是单网卡旁路网关示例，按实际局域网修改：

| 对象 | IPv4 / 设置 |
| --- | --- |
| 主路由器 | `10.0.0.2` |
| client 虚拟机 | `10.0.0.1/24`，网关 `10.0.0.2` |
| client 管理页面 | `https://10.0.0.1/`，默认 HTTPS 443 |
| 首台测试设备 | 例如 `10.0.0.50/24`，网关和 DNS 均设为 `10.0.0.1` |
| NAS 管理地址 | 保持原来的独立地址，不与 client 共用 `10.0.0.1` |

当前安装器把掩码固定为 `255.255.255.0`（`/24`），要求 client 与上游网关在同一子网，且地址不同。非 `/24` 网络不能直接照抄本文安装参数。先确认 `.1` 未被其他设备占用，并从路由器 DHCP 动态分配范围中排除。

本文验收以 IPv4 为准。IPv6 透明代理尚未完成端到端验证，双栈设备可能通过 IPv6 使用其他出口，需单独验证；初次验收可使用仅启用 IPv4 的测试设备。

可按 **2 个 vCPU、2 GB 内存、32 GB 系统盘、1 张网卡**创建专用虚拟机。这是部署参考配置，不是性能保证；已有 1 GB 内存的 NAS 客户端完成过实际验证。项目支持的 CPU 架构不等于所有 NAS 都支持 Virtualization Station，需以 NAS 实际能创建的虚拟机为准。

安装器会配置 Debian 软件源、DNS、网络、透明代理和防火墙，并安装 Nginx、PHP 8.4、Pi-hole 6 等组件。请在专用 Debian 虚拟机中运行，保留 Virtualization Station 控制台入口，便于地址切换后接续。

## 2. 在 QNAP 创建 Debian 虚拟机

### 2.1 连接局域网

1. 在 QTS 打开“网络与虚拟交换器”，进入“网络 → 虚拟交换器”，检查是否已有连接局域网物理网口的交换器。
2. 已有合适的桥接交换器时直接复用。需要新增时，通过“添加”向导选择接入主路由局域网的物理网口，并按现有 NAS 地址规划配置交换器。
3. 本方案使用桥接连接，不启用额外 NAT；局域网已有 DHCP 时，不在新交换器再开 DHCP 服务。NAS 管理地址保持原规划，**不要把 client 的 `.1` 填为 NAS 或交换器管理地址**。
4. 在 Virtualization Station 创建虚拟机，将其网卡连接至该交换器；网卡型号可选 Debian 支持的 VirtIO。

此方案通过虚拟交换器连接一张虚拟网卡即可，不需要 PCIe 网卡直通。桥接连接允许 NAS 与虚拟机共享物理接口；没有物理接口的隔离网络不能直接承担局域网旁路网关。参见 [QNAP 网络模式说明](https://www.qnap.com/en-us/how-to/tutorial/article/networking-modes-of-virtualization-station)。

QTS 5.2.x 的入口和向导选项见 [QNAP 创建虚拟交换器文档](https://docs.qnap.com/operating-system/qts/5.2.x/en-us/creating-a-virtual-switch-in-advanced-mode-711BFFC6.html)。实际按钮名称随应用版本可能不同，以“虚拟机和主路由处在同一局域网”为验收条件。

### 2.2 安装 Debian

1. 从 [Debian 官方安装页面](https://www.debian.org/distrib/) 获取 **Debian 13** ISO，核对下载的版本与虚拟机架构；常见 x86 NAS 虚拟机使用 amd64 ISO。
2. 将 ISO 挂载到虚拟光驱，启动控制台，安装最小系统，勾选“SSH server”和“standard system utilities”，无需桌面环境。
3. 网络安装阶段优先直接配置最终地址 `10.0.0.1/24`、网关 `10.0.0.2`；安装前的 DNS 可使用主路由提供的有效 DNS，后续由 client 安装器配置本地解析。
4. 安装完卸载 ISO，从系统盘启动。使用 Debian 管理账号登录；以下 Linux 管理命令均在 **Debian 虚拟机的 root shell** 中执行，普通管理账号先运行 `sudo -i`。不要在 NAS 的 QTS SSH shell 执行安装命令。
5. 先保留一张接入局域网的网卡。安装器自动选择首个处于 UP 状态的非回环接口，多网卡需先核对，避免选中其他网络。

若已通过 DHCP 得到临时地址，可以先经临时地址登录，但最终仍以 `.1` 为准。安装器写入静态配置不代表运行中的地址已经切换；安装完成后必须重启并核对。为减少中途地址不一致，建议新装 Debian 时就配置最终地址。

## 3. 从 server 获取节点

通过 SSH 登录已经部署好的 server，核对主机身份。运行该 server 使用的 `server` 脚本，选择 **`11. Print Xray node information`**，记录输出的 DoH、Domain、UUID、Path。

不要为“查看节点”选择重新安装或更换 UUID/Path。若输出多个 UUID，选择其中一个当前有效的 UUID，不要把多行合并输入 client。

| 项目 | 示例 | 核对方式 |
| --- | --- | --- |
| 节点传输 | VLESS + WebSocket + TLS | 本项目标准服务端配置 |
| Domain | `node.example.com:443` | 域名和端口与 server 当前入口相同 |
| TLS / SNI | `node.example.com` | 安装器使用 Domain 中的主机名，证书必须匹配 |
| UUID | server 输出的一个有效 UUID | 原样填写 |
| Path | server 输出的完整 Path | 原样填写，保留开头 `/` 和输出中的查询参数 |
| DoH | `node.example.com/dq` | 安装器输入格式省略 `https://`；非默认端口保留 `:端口` |

浏览器访问的 DoH URL 是 `https://node.example.com/dq`，但 client 的 **`DoH / xDNS` 提示中应填写 `node.example.com/dq`**。安装器会自行构造 HTTPS 请求；填入带协议头的 URL 会导致域名解析参数错误。已有 xDNS 部署可使用 server 提供的 `域名:端口`，不要将任意公共 DoH 路径替换为本项目的 `/dq`。

在 client 中做连通性预检查，先把示例域名换成真实域名：

```bash
apt-get update
apt-get install -y ca-certificates curl
getent ahostsv4 node.example.com
curl --connect-timeout 10 --max-time 20 -I https://node.example.com/
```

HTTPS 请求应能完成连接与证书校验。首页 HTTP 状态只说明该地址有响应，不能单独证明 VLESS 或 DoH 可用。当前安装提示面向本项目节点，不是通用订阅、Reality、gRPC 或其他协议的导入入口。

## 4. 安装 client

### 4.1 系统检查

```bash
cat /etc/os-release
dpkg --print-architecture
ip -br address
ip -4 route
timedatectl status
df -h /
```

应看到 Debian 13 / `trixie`、`amd64` 或 `arm64`、正确的局域网接口与默认网关。系统时间应准确且 NTP 已同步，以免 TLS 校验失败。

Debian 11/12 不能直接运行本版安装器完成系统迁移。保留旧虚拟机，新建 Debian 13 VM 是一种迁移方式；需要原地升级时，先备份，再按 **11 → 12 → 13** 分阶段完成包配置、重启和网络检查，最后更新应用。

### 4.2 下载安装入口

在 root shell 中逐步执行，每一步成功后再继续：

```bash
apt-get update
apt-get install -y ca-certificates curl
curl --fail --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 15 --max-time 120 \
  https://raw.githubusercontent.com/XDcooler/mygwd/main/client \
  -o /root/de-gwd-client.sh
bash -n /root/de-gwd-client.sh
bash /root/de-gwd-client.sh
```

下载或语法检查失败时先处理错误，不运行残缺脚本。上述语法检查只检查 Bash 语法，不代替来源及内容核对。

如果希望使用随仓库附带并可校验的路由快照，可以改用完整源码安装，**两种方式选一种**：

```bash
apt-get install -y ca-certificates git
git clone https://github.com/XDcooler/mygwd.git /root/mygwd
cd /root/mygwd
bash client
```

该方式仍需联网安装 Debian 依赖和刷新规则，不是离线安装。GitHub 下载不可达时，也可在另一台电脑下载本仓库完整源码并通过 SCP 传入虚拟机，保持 `client` 与 `resource/` 的目录关系。

### 4.3 按提示输入

菜单选择 **`1. Install de_GWD`**，随后依次填写：

| 顺序 | 安装器提示 | 本文示例 / 填写要求 |
| --- | --- | --- |
| 1 | `de_GWD local IP address` | `10.0.0.1`，不带 `/24` |
| 2 | `Upstream route IP address` | `10.0.0.2` |
| 3 | `DoH / xDNS` | `node.example.com/dq`，不带 `https://` |
| 4 | `Domain` | `node.example.com:443`，不带 `https://` |
| 5 | `UUID` | 从 server 取得的有效 UUID |
| 6 | `Path` | 从 server 取得的完整 Path |

首次安装随后会出现 `Web management password` 和确认提示，交互输入独立的面板密码。已有有效配置时会保留已有密码散列，不一定再次询问。**面板密码、Debian SSH 密码、节点 UUID 是三种不同用途的凭据。**

等待明确出现 **`Install de_GWD [done]`**。菜单会再次显示，看到菜单本身不能当作安装成功。如果中途报错，先按第 9 节查看服务和日志，不反复选择全新安装覆盖现场。

### 4.4 固定地址与冷启动

安装成功、没有其他包管理任务运行后，退出菜单，在 Debian 控制台执行：

```bash
reboot
```

虚拟机重新启动后，通过控制台核对地址，再从电脑 SSH 连接最终地址：

```bash
ssh root@10.0.0.1
```

若 root SSH 登录未启用，使用已有管理账号登录并 `sudo -i`，不必为了部署开放 root 远程登录。静态地址、默认路由应分别为 `10.0.0.1/24`、`via 10.0.0.2`，不再依赖原 DHCP 地址。

## 5. 核对服务、DNS 和后台

### 5.1 七项服务

```bash
for unit in nginx php8.4-fpm pihole-FTL smartdns v2dns vtrui iptables-proxy; do
  printf '\n%s\n' "$unit"
  systemctl is-active "$unit"
  systemctl is-enabled "$unit"
done
systemctl --failed --no-pager
ip -br address
ip -4 route
ss -lntup
```

七项服务应为 `active` 且已启用开机启动。`iptables-proxy` 是 oneshot 服务，**`active (exited)` 正常**，不能据此判定未启动。Debian 13 使用 `php8.4-fpm`；升级后旧 `php7.4-fpm` 停用是正常结果，不需要重新启动。

| 组件 | 正常部署的主要端口 / 作用 |
| --- | --- |
| Nginx | HTTPS 443；HTTP 80 跳转 HTTPS |
| PHP 8.4 FPM | Unix socket `/run/php/php8.4-fpm.sock` |
| Pi-hole FTL | 局域网 DNS：TCP/UDP 53；内置管理服务仅回环 8086 |
| SmartDNS | 5310、5311、5320、5321，内部解析链 |
| V2DNS | `127.0.0.1:5350`，内部解析链 |
| Xray / vtrui | `127.0.0.1:9896`，透明代理入口 |

安装引导阶段会暂用 Pi-hole 1053；最终 LAN DNS 应回到 53。看到 1053 残留应检查安装收尾是否失败，不直接把所有 DNS 服务改为同一个端口。

### 5.2 DNS 与本机 HTTPS

安装完成后可使用 `dig` 检查两种 DNS 传输：

```bash
dig @127.0.0.1 debian.org A +time=3 +tries=1
dig @127.0.0.1 debian.org A +tcp +time=3 +tries=1
```

两者应能返回有效解析结果；若未安装 `dig`，安装 `dnsutils`。还需在局域网另一台设备重复检查 `@10.0.0.1`，确认桥接网络和 LAN DNS 监听可用。

默认自签证书含 `localhost` 和 `127.0.0.1`，可在 VM 内执行严格 HTTPS 检查：

```bash
curl --cacert /var/www/ssl/de_GWD.cer \
  --connect-timeout 5 --max-time 15 -I https://localhost/
```

登录页或登录重定向均属正常响应。已有自定义证书时，改用证书实际覆盖的主机名和相应 CA。随后通过 `https://10.0.0.1/` 打开后台，使用安装时设置的面板密码登录，检查概览、节点和服务分流页面。

默认自签证书没有覆盖 `10.0.0.1`，直接按该 IP 访问可能同时出现信任和名称不匹配提示。按附录配置覆盖该 IP 的证书，并通过已核对的 SSH/控制台确认指纹后使用；不要以关闭证书校验作为验收依据。

忘记面板密码时，在 VM root shell 交互重设：

```bash
/opt/de_GWD/ui-piholeConfig password
```

也可运行安装入口选择 `9. Change de_GWD password`。不要把新密码放进命令参数或公开配置示例。

## 6. 接入设备并验证与 server 的连通性

先只修改一台测试设备，确认可用后再扩展到其他设备：

1. 测试设备使用与 client 同网段的唯一地址，例如 `10.0.0.50/24`。
2. 将该设备的 **默认网关、DNS 都设为 `10.0.0.1`**。仅修改 DNS 不代表所有流量经过 client。
3. 测试时关闭该设备上另一个 VPN/代理；浏览器自带加密 DNS 等设置也应与测试方案一致，避免绕过预期 DNS 链。
4. 访问 client 后台、局域网设备、国内站点和境外站点，分别记录结果。

在 client 和测试设备上分别执行以下下载测试；电脑需有 curl：

```bash
curl --fail --location --connect-timeout 10 --max-time 30 \
  --output /dev/null \
  --write-out 'HTTP=%{http_code} bytes=%{size_download} seconds=%{time_total}\n' \
  https://raw.githubusercontent.com/XDcooler/mygwd/main/README.md
```

应返回 HTTP 200 且下载字节数大于零。再对一个已明确设为“指定节点”的境外服务建立新连接，并对照 server 的连接/访问日志确认进入该节点。单纯 ping 通 server 或打开 server 首页，只证明部分链路可达，不能代替透明代理测试。

需要比较出口时，可对出口检测域名建立临时“指定节点”策略后查询出口，并与 server 的实际公网出口对照；测试完恢复策略。没有明确策略时，某个查询站点得到直连出口也可能是分流结果，应先检查规则命中。

在概览页开启 UDP，再验证代理 UDP 和常用 DNS 的 UDP/TCP。实物游戏机还需实测登录、商店、联机和 NAT 类型；流媒体/AI 还需使用相应账号验证访问权限。节点可连接、规则能命中不等于地区解锁或账号可用。

## 7. 配置服务分流和定期更新

登录“服务分流”，按流媒体、AI、游戏平台搜索项目，选择“跟随默认 / 直连 / 指定节点”，再点击“保存并应用”。新连接使用新策略，已有连接可能继续使用原出口。

游戏机在主路由保留固定 IPv4，并将网关、DNS 指向 client。在对应平台卡片填写主机 IP，选择节点，开启 UDP。每个主机 IP 只分配给一个游戏平台；留空时按平台域名匹配。

当前包括 12 项流媒体、9 项 AI、5 项游戏平台，完整列表和规则行为见 [服务分流说明](debian13-routing.md)。

GeoIP、GeoSite、中国 IPv4 路由和广告规则默认每天 **北京时间 04:00** 刷新。可在页面手动触发并查看结果，也可在 VM 中执行：

```bash
/opt/de_GWD/ui-updateRules
```

检查 `timedatectl` 的时区、`systemctl is-active cron` 和 root 的 `crontab -l`，确认维护入口存在；不要重复添加第二份定时任务。规则校验/应用失败会保留或恢复此前配置，状态页应能看到错误。来源和校验机制见 [规则更新说明](rule-updates.md)。

项目版本轮询和自动远程代码覆盖已经移除。**规则更新不会自动升级项目代码。** 手动更新项目时，重新下载当前安装入口并检查成功，运行后选择 `0. Update de_GWD`。其中 `Update Debian System` 只是在已支持的 Debian 13 中更新软件包，不是 Debian 11→13 的跨版本升级工具。

新入口会先准备、校验下载资源，再停止代理重建；准备失败不主动停掉现有网关。后续重建仍有服务中断，应在维护时间进行。

## 8. 备份与验收

升级应用或修改网络前，在虚拟机平台创建可恢复的备份/快照，并确认可从 NAS 控制台恢复。配置备份至少应覆盖以下内容：

- `/etc/`：网络、systemd、Nginx、PHP、Pi-hole、SmartDNS、防火墙等配置。
- `/opt/de_GWD/`：真实节点、面板密码散列、分流及更新配置。
- `/var/www/html/` 和 `/var/www/ssl/`：后台及证书、私钥。

备份包含凭据和证书私钥，应限制权限并保存在私有位置，不能提交到 GitHub。恢复旧 VM 时避免让新旧两台同时使用 `.1`；恢复配置须匹配 Debian/PHP/Pi-hole 版本，不把 Debian 11 的整个 `/etc` 直接覆盖到 Debian 13。

部署完成逐项确认：

- [ ] 冷启动后固定 IPv4、默认网关正确，SSH/控制台可用。
- [ ] 七项服务 active 且启用自启，无未处理的失败单元。
- [ ] 本机与 LAN 设备的 UDP/TCP DNS 都正常。
- [ ] 后台 HTTPS 证书已核对、密码可登录，服务分流可以保存。
- [ ] LAN 测试设备的国内/私网直连及指定节点连接均按策略工作。
- [ ] UDP 按需开启并实测；下载成功且不是只测 ping。
- [ ] 手动规则刷新成功，04:00 维护入口存在，重启后策略保留。
- [ ] 实测结果明确区分通过、失败、未测及环境限制。

已完成的 Debian 13 实际验证及平台未测边界见 [验证记录](debian13-validation.md)，不能直接替代新环境的验收。

## 9. 故障排查：先查服务未启动原因，再查下载

下载卡住时先记录服务状态和第一条实际错误，不先停掉 DNS/代理，也不反复执行全量安装。

```bash
systemctl --failed --no-pager
systemctl status smartdns v2dns vtrui iptables-proxy --no-pager --full
journalctl -b -u smartdns -u v2dns -u vtrui -u iptables-proxy \
  --no-pager -n 100
systemctl status nginx php8.4-fpm pihole-FTL --no-pager
nginx -t
timedatectl status
ip -4 route
ss -lntup
```

进一步对失败单元执行 `systemctl cat 单元名`，核对 `ExecStart` 的程序是否存在、配置是否有效、端口是否冲突以及依赖服务是否就绪。修复具体原因后再启动相应单元，验证 DNS、节点连接及下载。分享日志前遮盖真实 UUID、路径、密码和令牌。

| 现象 | 处理方向 |
| --- | --- |
| SSH `No route to host` / 超时 | 用 QNAP 控制台检查 VM 是否启动、实际 IP、桥接网口、VLAN、地址冲突及访问端防火墙；先区分客户端应用访问权限与 VM 网络故障 |
| `.1` 不出现或重启变回 DHCP | 检查 `/etc/systemd/network/05-de-gwd-client.network` 或 `/etc/network/interfaces`，核对接口名；排查 cloud-init/其他网络管理器覆盖，不并行启用多套配置 |
| 服务 `203/EXEC` | 检查 ExecStart 路径、文件存在性、执行权限与架构；Debian 13 的旧 PHP 7.4 单元不要重新启用，应确认 Nginx 使用 PHP 8.4 socket |
| `iptables-proxy active (exited)` | oneshot 的正常状态；继续核对防火墙、策略路由及实际连接，不把它当作崩溃 |
| DNS 不通、FTL 仍在 1053 | 检查安装是否在 DNS 收尾前中断；最终 FTL 应使用 53、V2DNS 使用回环 5350，先排除端口冲突与配置错误 |
| 节点连接失败 | 核对 server 当前 UUID/Path/端口、DoH 输入格式、TLS 域名和证书、系统时间；同时查看 server 服务与日志 |
| 网页 502 | 检查 `php8.4-fpm`、socket 和 Nginx 错误日志；不要安装旧 PHP 7.4 来覆盖问题 |
| 分流保存 / UDP 操作失败 | 检查登录会话、错误提示及 `/etc/sudoers.d/de-gwd-routing`；分流/UDP 使用六个固定后台操作，旧管理按钮使用无 CLI 参数的 `ui-panel` 入口，不给 www-data 通配 root 权限 |
| 服务都停了且下载不动 | 旧更新流程可能先删核心/停服务，再下载失败；结合日志确认，使用含随包资源的当前源码在维护窗口恢复，勿把“缺核心”仅当网络问题 |
| DNS 正常但下载仍卡住 | 服务正常后，对同一 URL 做带连接/总时限的 HTTPS 下载；区分解析、TLS、HTTP 和传输超时，保留实际错误及字节数 |
| APT 锁被占用 | 检查另一 apt/dpkg 或 apt-daily 是否仍运行，等待其完成；不删除锁文件或同时启动第二个升级 |
| 某个公共 UDP DNS 超时 | 比较其他 DNS 与经节点 UDP，不能据单个地址判定全部 UDP 故障；既有 NAS 环境曾仅直连 `1.1.1.1:53/UDP` 受限 |

## 附录：为局域网 IP 配置自签证书

仅在默认证书不覆盖实际 client IP、且准备使用自签证书时执行。以下命令在 VM 的 **Bash root shell** 中运行，示例 IP 必须先改为实际 client 地址。已有正式证书时优先保持原证书和续期方案。

先备份证书对，在临时目录生成覆盖 IP 的证书，验证后安装：

```bash
(
  set -e
  umask 077
  cert_work=$(mktemp -d /root/de-gwd-cert.XXXXXX)
  cp -a /var/www/ssl/de_GWD.cer "$cert_work/previous.cer"
  cp -a /var/www/ssl/de_GWD.key "$cert_work/previous.key"
  openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 365 \
    -subj '/CN=10.0.0.1' \
    -addext 'subjectAltName=IP:10.0.0.1,DNS:localhost,IP:127.0.0.1' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
    -addext 'extendedKeyUsage=serverAuth' \
    -keyout "$cert_work/new.key" -out "$cert_work/new.cer"
  openssl x509 -in "$cert_work/new.cer" -noout -checkip 10.0.0.1
  if install -m 0600 "$cert_work/new.key" /var/www/ssl/de_GWD.key &&
     install -m 0644 "$cert_work/new.cer" /var/www/ssl/de_GWD.cer &&
     nginx -t && systemctl reload nginx; then
    openssl x509 -in /var/www/ssl/de_GWD.cer -noout -fingerprint -sha256
    printf 'Certificate backup: %s\n' "$cert_work"
  else
    cp -a "$cert_work/previous.key" /var/www/ssl/de_GWD.key
    cp -a "$cert_work/previous.cer" /var/www/ssl/de_GWD.cer
    nginx -t && systemctl reload nginx
    printf 'Certificate update failed; previous files restored. Backup: %s\n' "$cert_work" >&2
    exit 1
  fi
)
```

该证书有效期 365 天，应在过期前更换。备份目录含私钥，保持私有。在已确认身份的 SSH/控制台核对 SHA-256 指纹，再将 **公开证书 `de_GWD.cer`** 传到管理电脑核对；不要复制 `.key`。管理电脑可以对单次请求指定该公开证书：

```bash
curl --cacert ./de_GWD.cer --connect-timeout 5 --max-time 15 -I https://10.0.0.1/
```

浏览器使用经核对的站点证书信任/例外，或安装由受信任 CA 签发且包含访问名称的证书。只增加 IP SAN 不能消除自签证书的信任提示；证书链信任与名称匹配都应正确。

证书检查命令面向 Debian 13 的 OpenSSL；`-checkip` 参数见 [OpenSSL x509 官方文档](https://docs.openssl.org/3.5/man1/openssl-x509/#certificate-checking-options)。macOS 自带 LibreSSL 的命令选项可能不同，应在 Debian VM 内运行部署命令。
