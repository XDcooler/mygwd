# Debian 13 与服务分流

安装支持 Debian 13 (trixie)，amd64 / arm64，systemd。客户端使用发行版 PHP 8.4 FPM（≤1.5GB 内存最多3个按需进程，其他机器8个），Pi-hole 6 的 DNS 和内置管理服务；Nginx 将 `/admin/` 和 `/api/` 转发到仅监听回环的 8086 端口。安装器识别 systemd-networkd 与传统静态网络配置，固定客户端地址在重启后保持。现有 Debian11 系统按 11→12→13 升级，每阶段完成包配置与重启检查，再迁移应用。

## 配置服务

登录面板后打开“服务分流”。26 项服务分为 12 项流媒体、9 项 AI、5 项游戏平台，支持搜索和对当前筛选结果批量配置。每项可以跟随默认节点、直接连接或选择已保存的节点。点击“保存并应用”后，新连接采用策略；已有连接可能维持原出口。节点身份由地址和传输参数计算，节点改名不会丢失选择。节点被删除时会提示重新选择，不会静默切换地区。

游戏主机先保留私网固定 IPv4，将网关和 DNS 指向本客户端，在对应卡片输入主机 IP，并开启概览页 UDP。填写主机 IP 后，其公网流量采用该平台策略，局域网访问保持直连；留空则按平台域名匹配。一个 IP 只能属于一个游戏平台。

流媒体包括 Netflix、Disney+、YouTube、Prime Video、HBO/Max、Apple TV+、Spotify、Hulu、Twitch、Paramount+、Peacock、Crunchyroll。AI 包括 OpenAI、Anthropic、Gemini、Microsoft Copilot、GitHub Copilot、Perplexity、xAI、Hugging Face、Poe。游戏包括 PlayStation、Xbox/Game Pass、Nintendo/Switch、Steam/Deck、Epic。完整规则由 `service-catalog.json` 管理。

## 规则更新与恢复

升级先保持现有 DNS、代理和 cron 运行，下载并校验核心包、代理脚本和路由，准备失败不进入停服与清理阶段。重建时直接使用已缓存的代理脚本。规则更新前的时间检查限时读取系统 NTP 状态，不通过 HTTP 校时，不阻塞 DNS 最终配置。

本地源码安装先验证随包路由快照的 SHA-256 与 CIDR，用于代理重建阶段；网关恢复后再完成在线规则刷新。依赖安装会统一 Debian 13 官方源，清理旧 PHP 外部源，即使跳过整机包升级也会修正旧安全源路径。

每天北京时间 04:00 刷新 GeoIP、GeoSite、中国 IPv4 路由和广告源，可在分流页手动触发并查看进度。来源固定到解析出的完整提交，并校验散列与内容格式；服务域名类别消失时使用显式域名补充。编译器将域名和专用 IP 规则分开，避免误用 AND 条件，DNS ipset 与主机源 IP 集合保证选定服务能进入透明代理。

配置先由真实 Xray 检查，失败不替换工作文件。应用或数据更新失败会恢复此前文件，并尝试恢复服务。规则更新与网页保存使用相同部署锁。状态与节点下拉列表不会返回 UUID、WebSocket 路径或面板密码。项目版本检测和自动下载覆盖代码已移除；管理员仍可主动更新项目，系统依赖与规则更新保留。

## 管理权限

Pi-hole 6 不再替 PHP 面板提供旧提权配置。除分流状态/保存、无参数规则刷新、UDP状态/开/关六项固定操作外，安装器还授权一个无命令行参数的 `/opt/de_GWD/ui-panel` 管理入口，写入 `/etc/sudoers.d/de-gwd-routing` 并由 visudo 校验。入口只接受标准输入中的固定动作与经校验的参数，不接受任意 shell、服务名或下载 URL，也不给 www-data 通配 root 权限。

节点、DNS/DHCP、转发、备份和更新等旧按钮通过该入口执行，写操作统一 POST + CSRF，失败返回非 2xx 并在页面提示。`0conf`、备份和核心配置由 root 私有保存，PHP 通过受控接口读取，Web 源码和 helper 不允许 www-data 改写。备份下载要求登录与短期会话授权，文件不放在 Web 根目录。

更新/安装控制台由 systemd `Type=simple`、`KillMode=control-group` 管理，只监听受权限保护的 Unix socket，经 Nginx 的登录会话与一次性连接授权提供访问，不再暴露 TCP 3000。控制台最长运行 15 分钟；相关改动按本轮要求未运行测试，详见 [面板报告修复说明](panel-report-fixes.md)。

可选 Docker 使用官方签名源和 Compose 插件；Jellyfin 使用限定签名源。FileRun 改为 PHP8.4，需管理员通过 `DE_GWD_FILERUN_PACKAGE` 提供当前授权安装 ZIP，ionCube 从官方 HTTPS 下载。可选应用未随网关测试自动安装。内核加速入口启用 Debian 自带 BBR，不再安装旧 5.10 自定义内核。

## 验证边界

路由配置加载、DNS、Web 保存与真实节点 TCP/UDP 转发，与平台地区解锁、付费订阅可用性及实物主机 NAT 类型是不同结果。后者需要相应账号和设备，不能用域名匹配成功替代。测试报告须分别记录通过、失败和环境限制。
