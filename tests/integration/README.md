# Debian 11 真实集成测试

此测试在可销毁的 Debian 11 虚拟机里执行，使用真实 systemd、Xray、Pi-hole、
iptables TPROXY、ipset、policy routing 和 veth/network namespace。PRoot 和
fake sudo 测试无法替代这里的内核及网络验证。

## 使用已部署的真实节点

`test_real_gateway.py` 保留客户端已安装的真实 VLESS+WebSocket+TLS 节点配置，
要求证书校验开启。先完成真实服务端和客户端部署，再在本地 Debian 11 VM 执行：

```sh
python3 tests/integration/test_real_gateway.py \
  --report-dir /tmp/de-gwd-real-integration-$(date -u +%Y%m%dT%H%M%SZ) \
  --expected-exit YOUR_REAL_SERVER_PUBLIC_IP
```

默认运行 23 项检查：真实 TCP/UDP 代理出口、中国及私网直连、正常 DNS、
Pi-hole UDP/TCP 广告阻断、查询日志、UDP 开关、服务重启及外部内核规则保留。
分别测量 TCP 和 UDP 的直连基线，不要求上游网络为两种传输提供同一个出口。
公网探测使用 Cloudflare 的 HTTPS trace 和 UDP DNS；需要 VM 可以访问这些端点。
中国地址从当前 chnrouteSET 选择后仅在测试 namespace 中提供 HTTP/UDP 服务，
该辅助后端不运行代理节点。报告保存观测出口、规则快照与广告日志，不保存节点 UUID 或密码。
报告会含运行环境的公网出口与 LAN 测试地址，公开分享前应检查内容。

若已单独获准使用本地面板凭据，可追加 `--access-file /path/to/access.json`。
JSON 的 `password` 字段仅在内存读取；启用真实登录、Cookie、页面、网页 UDP 开关和注销检查。
默认访问 `https://localhost` 并信任 `/var/www/ssl/de_GWD.cer` 这个固定公开证书，
不会关闭 TLS 校验。没有该参数时，通过命令行辅助脚本操作 UDP，不能把结果写成网页操作通过。

本次在电脑本地 Debian 11 ARM64 VM 的真实节点网络检查 23 项通过，冷启动后再次通过。
单独授权使用面板凭据后，真实 HTTPS 登录、七个页面、网页 UDP 开关和注销等完整 26 项通过；
测试结束删除 VM 内临时密码文件。IPv6、可选应用和内核升级不在这些检查覆盖内。

## 隔离的本地节点场景

先部署客户端，节点使用受控本地 VLESS：`198.18.231.2:14443`，UUID 从客户端
部署配置读取。真实 `vtrui`、`iptables-proxy`、`pihole-FTL` 应已启动；
`/opt/de_GWD/ui-proxyRules`、`ui-setUDP` 和广告 gravity 数据库应已安装。

```sh
python3 tests/integration/test_gateway.py \
  --report-dir /tmp/de-gwd-integration-$(date -u +%Y%m%dT%H%M%SZ)
```

若已有本地测试密码文件，追加 `--web-password-file /path/to/fixture-password`
可同时检查真实 Nginx/PHP-FPM 登录、未认证动作拒绝、Cookie 属性、7 个页面、
网页 UDP 开关及实际流量与指示器一致性、重启后的登录状态和注销。密码只在
内存读取，不写入报告或请求 URL。默认 Web 地址是 guest 内 `http://127.0.0.1`；
通过 `--web-base-url` 指定其他本地 Web 监听地址。节点切换不在此测试中执行。

所需软件：Python 3（含 SQLite）、curl、jq、iproute2、iptables、ipset、
util-linux、systemd，以及客户端部署的 Xray 和 Pi-hole。必须以 root 在
Debian 11 guest 内运行；不要在现有生产网关运行。

测试自动建立 LAN namespace `192.168.231.2` 和 backend namespace
`198.18.231.2`；backend 没有默认路由。所有合成目标地址均被明确路由到
backend，并在其 loopback 上提供 HTTP、UDP 和 DNS 服务。HTTP 与 UDP
请求携带唯一 nonce；响应源地址须与先测得的精确源地址一致，同时核对
backend 访问日志。停止 upstream VLESS 时，代理目标必须失败，直连目标
仍须成功；恢复后再验证成功。客户端请求清空环境并显式禁用 curl 的代理。

覆盖内容包括：

- 每个目标的 TCP/UDP 直连基线、非中国 TCP/UDP 透明代理、中国与私网直连。
- 自定义目的地黑白名单、中国目的地强制代理、UDP 开关和实际 DNS 数据包。
- Pi-hole 正常域名解析和访问、真实 gravity 广告域名阻断、LAN 查询日志。
- 停止、启动、重复启动、客户端服务重启及错误配置拒绝时的实际行为。
- 外部 INPUT/OUTPUT/POSTROUTING 防火墙规则、独立 policy rule 和 table 220
  中独立 specific-prefix 路由保留。

测试结束恢复临时 DNS/名单/UDP 配置、原 ip_forward 值，停止测试进程并删除
本次 namespace 和哨兵规则。任何失败（包括清理失败）使执行返回非零状态。
报告目录须是一个尚不存在的目录；其中保存 JSON 逐项结果、Markdown 报告、
backend 日志、upstream Xray 日志与 Pi-hole DNS 查询证据。报告不保存 UUID。

本地 VLESS backend 使用受控明文协议，只用于隔离集成测试。通过该测试不表示
公网服务端、TLS/ACME、IPv6、客户端绕过网关 DNS 的防护或被托管网络拒绝的
远端 SSH/代理连接已经验证。与保留 table 220 独立路由不同，已有规则占用
de_GWD 的保留 mark 或 local-default 路由属于所有权冲突，未被该哨兵覆盖。
