# 回归验证

从仓库根目录执行：

```bash
python3 scripts/build-client-archive.py
python3 tests/test_regressions.py
python3 tests/test_rule_updates.py
python3 tests/test_client_updates.py
```

`test_client_updates.py` 在临时目录运行实际更新入口和面板 broker，使用服务命令适配器，
覆盖逐阶段失败、信号中断、cron 恢复、Xray 就绪和规则启动重试、空参数兼容、配置权限、
更新互斥及状态输出；还检查 PHP 部署刷新、失败后的 DNS 端口/系统解析器恢复，
以及 UDP/TCP DNS 真实应答判定。它不执行真实安装；真实 systemd 与完整更新仍需 Debian VM 验证。
更新的后台执行和恢复方式见 [客户端更新恢复](../docs/client-update-recovery.md)。

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
