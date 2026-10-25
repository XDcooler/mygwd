# 回归验证

从仓库根目录执行：

```bash
python3 scripts/build-client-archive.py
python3 tests/test_regressions.py
python3 tests/test_rule_updates.py
python3 tests/test_client_updates.py
python3 tests/test_update_sources.py
python3 tests/test_dns_settings.py
python3 tests/test_node_switch.py
node tests/test_node_switch_ui.js
python3 tests/test_server_installer.py
python3 tests/test_kernel_update.py
python3 tests/test_xanmod_install.py
```

服务端检查及 Debian 13 内核包安装验证见 [服务端 Bash 安装与内核更新](../docs/server-install-kernel.md)。
服务端 fixture 将所有写入放在临时目录；包管理、服务命令做适配，原生 Linux 测试使用随包 Xray 和本地 Nginx。
内核用例需分别以 root 与普通用户运行，分别覆盖安装分支和非 root 拒绝；没有在运行测试的宿主机更新内核。

`test_xanmod_install.py` 在普通用户的私有目录中验证两端菜单 2/3、服务端共享安装器加载与失败退出、XanMod 官方密钥指纹、LTS 安装、
架构/容器/UEFI 检查、新内核 BBR/fq 能力及模块启动加载、启动文件与默认 GRUB 目标、失败恢复和最后的重启请求。APT、GRUB 和重启均由命令适配器替代，
不在宿主机安装内核；这不代表已在真实 VM 启动过 XanMod。该用例与来源检查工作流一并运行。

`test_client_updates.py` 在临时目录运行实际更新入口和面板 broker，使用服务命令适配器，
覆盖逐阶段失败、信号中断、cron 恢复、Xray 就绪和规则启动重试、空参数兼容、配置权限、
更新互斥及状态输出；还检查 PHP 部署刷新、失败后的 DNS 端口/系统解析器恢复，
以及 UDP/TCP DNS 真实应答判定。它不执行真实安装；真实 systemd 与完整更新仍需 Debian VM 验证。
更新的后台执行和恢复方式见 [客户端更新恢复](../docs/client-update-recovery.md)。

`test_update_sources.py` 检查客户端与服务端的默认下载源、旧仓库环境变量迁移、固定提交与面板更新入口的独立性、
完整客户端更新时的配置迁移，并逐项核对安装 ZIP 与源码。全部下载和配置操作都使用临时文件与命令适配器，
不连接 GitHub、不重启宿主服务。对应 CI 为 `update-sources.yml`，发布后提交或 PR 改动相关文件时自动运行。

`test_node_switch.py` 使用私有目录和服务命令适配器，覆盖候选配置校验、默认出口及跟随出口同步、显式分流保留、
重启/监听/写入/中断失败恢复、旧分流标识恢复和被引用节点删除拒绝，不重启测试宿主机服务。
`test_node_switch_ui.js` 使用 Node.js 内置模块验证切换等待、失败、成功及非法索引，确保失败请求不会提前标记为已切换。
切换机制与验收方式见 [节点切换说明](../docs/node-switching.md)。

`test_dns_settings.py` 检查旧仓库更新元数据迁移、DoH 保存、备份恢复及自定义命令拒绝；Linux 上还以临时目录和命令适配器验证同 IP 更改 DoH 端口/SNI 后 SmartDNS 重载、重复应用及重启失败传播，不修改宿主机 DNS。

功能与安装器回归应在隔离 Debian 13 ARM64 或 amd64 VM 中执行，使用系统 PHP 8.4：

```bash
sudo apt-get install util-linux php8.4-cli php8.4-gd jq mawk moreutils git shellcheck
sudo mkdir -p /workspace
sudo env DE_GWD_ROOTFS=/ python3 tests/test_regressions.py
python3 tests/test_rule_updates.py
python3 tests/test_service_routing.py
python3 tests/test_transparent_proxy.py
python3 tests/test_pihole_install.py
```

`DE_GWD_ROOTFS=/` 在私有 mount namespace 中绑定 fixture，系统管理操作用命令适配器记录，结束时撤销挂载。旧 Pi-hole5 测试仍用于检查 Pi-hole6安装器复用的下载与配置完整性验证函数；Pi-hole6真实安装和配置另须在 Debian13 VM 执行。

测试在 `/workspace` 创建独立临时目录，将 `/opt/de_GWD`、`/etc`、session、上传目录和命令适配器绑定到测试 fixture，直接运行仓库 PHP 页面及接口。所有特权命令均由 fake sudo 记录参数并返回受控结果，测试不运行真实安装、恢复、DNS 或防火墙操作；结束时清理测试进程与目录。页面测试没有使用云环境的开发路由，能检查应用本身的鉴权。

覆盖全部 action 接口的匿名拒绝、cookie 伪造和过期、密码变更、登录 session 更新、cookie 属性、注销、错误及数组参数、命令注入、NFS 参数传递、升级下载顺序及失败保留、上传失败保留、DDNS 状态输出、uptime 边界、8 个页面的完整渲染及 PHP 警告。安装归档也逐文件校验，防止已修复源码未进入实际安装包。

在本地 Debian 13 VM 运行静态检查：

```bash
shellcheck --severity=error client server resource/client/ui-script/* scripts/install-pihole5.sh
```

其余隔离回归：

```bash
python3 tests/test_transparent_proxy.py
python3 tests/test_pihole_install.py
```

root 运行 Pi-hole 回归时，非 root 拒绝检查会跳过，需再以普通用户运行该方法。
这些隔离测试分别覆盖规则更新、透明代理脚本和 Pi-hole 安装器，不替代真实网络测试。
实际 systemd、Nginx/FPM、DNS 和透明 TCP/UDP 的运行方式见 [真实集成测试](integration/README.md)。
2026-10-18 已在电脑本地 Debian 11 ARM64 VM 使用公网真实 TLS 节点通过 23 项网络检查，
并验证冷启动恢复；取得独立面板凭据授权后，包含真实登录、七个页面、网页 UDP 开关、
会话及注销的 26 项集成全部通过，测试后删除 VM 内密码副本。

规则更新测试使用 Python 标准库、临时目录和 SQLite fixture，不联网刷新数据，也不调用实际 Pi-hole 或 ipset。它验证数据格式和校验和、失败保留、并行互斥、路由交换、广告源注册及配置保留，以及已有 amd64/arm64 归档中核心未被替换。重新获取上游快照使用 `python3 scripts/update-rule-data.py`，命令和来源见 [规则更新说明](../docs/rule-updates.md)。

Debian13真实节点集成脚本支持 `--shared-upstream-gateway PRIVATE_IP`：仅在已独立确认宿主网关也使用同一代理出口时使用。出口相同的基线记为 `limited`，不会计为通过；其他网络功能仍必须实际通过。没有该选项时仍严格要求直连与代理出口可区分。物理主机、付费平台解锁及可选应用不属于这些检查的通过项。

本次 Debian 13 的隔离验证与公网/NAS 升级结果见 [部署验证记录](../docs/debian13-validation.md)，其中原始失败、环境限制和未测场景分别列出。

客户端升级的准备失败回归涵盖下载、坏 ZIP、核心成员缺失、代理脚本下载和规则失败五种场景，验证实际升级入口在失败时不清理核心、不停止服务、不改 DNS。故障注入在隔离 VM 执行；生产 NAS 只做无停服下载及散列核验。

时间检查回归确认同步、未同步、状态查询失败时均不抓取 HTTP Date、不设置系统/硬件时钟；状态检查最多 5 秒，由 Debian 自身时间服务负责同步。
