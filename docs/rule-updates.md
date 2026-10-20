# IP 地址库与广告过滤更新

本次仓库快照更新于 2026-10-16，完整来源提交、文件大小和 SHA-256 见 [`resource/rules/manifest.json`](../resource/rules/manifest.json)。

| 数据 | 来源 | 当前快照 |
| --- | --- | --- |
| `geoip.dat` | [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat) 的 release 分支 | 260 个分类，包含中国、私有地址及 IPv4/IPv6 数据 |
| `geosite.dat` | 同上 | 1,552 个分类，保留项目需要的 cn、private、apple、apple-cn、netflix 与 category-ads-all |
| 中国 IPv4 路由 | [misakaio/chnroutes2](https://github.com/misakaio/chnroutes2) | 3,879 条有效 CIDR，替换旧 jacyl4/chnroute 来源 |
| 全球广告域名 | [StevenBlack/hosts](https://github.com/StevenBlack/hosts) 基础列表 | 73,633 个域名，不增加其成人内容等可选扩展 |
| 中文广告域名 | [anti-AD](https://github.com/privacy-protection-tools/anti-AD) 域名列表 | 110,164 个域名 |
| 合并广告快照 | 上述两份列表取并集，排除 localhost 与地址占位项 | 178,927 个域名 |

上游数据的授权条款及署名以各来源仓库为准，来源和具体提交已保存在清单中。广告快照用于记录、校验和审阅；实际 Pi-hole 拦截由 `adlists.list` 中的两份上游列表在 gravity 更新时生成，数量会随上游和用户允许列表变化。

## 刷新仓库中的数据

从 checkout 根目录执行：

```bash
python3 scripts/update-rule-data.py
```

此命令优先通过 [GitHub HTTPS 提交 API](https://docs.github.com/en/rest/commits/commits#get-a-commit) 解析各上游分支的完整提交号，API 不可用时回退到 Git，通过 HTTPS 获取同一提交的数据与 GeoIP/GeoSite 校验和，校验 protobuf 分类、IPv4 CIDR 及广告域名格式，然后更新 `resource/rules`。它还同步 amd64、arm64 归档中的 GeoIP/GeoSite 数据，并重建客户端归档；核心二进制保持不变。

下载、校验或数据格式失败时，不替换之前的数据。更新目录有互斥锁，防止并行刷新相互覆盖。文件采用同目录临时文件及原子替换；清单最后写入。`--routes-only` 只刷新路由，保留其他数据；相应清单记录该次刷新的内容。

## 已安装机器上的刷新

在安装了本版客户端或服务端的 Debian 13 机器上执行：

```bash
sudo /opt/de_GWD/ui-updateRules
```

运行要求包括 Python 3（标准库即可）、Git、curl，以及实际应用时的 root 权限、ipset 和 Pi-hole。安装器会准备 Python 3 和更新入口，优先读取当前 checkout 中的归档；远程资产默认来自 `XDcooler/mygwd`，可以通过 `DE_GWD_RULES_REPOSITORY`、`DE_GWD_RULES_BRANCH` 指定仓库和分支。远程安装时，所选分支须已包含安装器及配套资产。

部署会更新 `/opt/de_GWD/v2dns`、`/opt/de_GWD/vtrui` 的数据，使用临时 ipset 装载后交换 `chnroute`，移除过期路由而不先清空正在使用的集合。ipset 装载失败时保留原集合。随后新增两份 Pi-hole 广告源并执行 `pihole -g`，重启已处于 active 状态的相关代理/DNS 服务。

Pi-hole 5/6 使用 `gravity.db` 的 adlist 表；旧版或首次安装使用 `adlists.list`。重复更新不产生重复记录，已有用户源、禁用状态、分组和允许列表保持不变。不存在 Pi-hole 时只保存源，并明确报告未激活过滤；不存在 ipset 时只准备路由文件。更新会重新编译已选的 26 项服务分流；域名和专用 IP 分开匹配。应用失败会恢复规则文件、核心配置和相关集合文件，并尝试恢复已运行服务，错误状态可在前端查看。新网页保存与规则更新共享部署锁，避免覆盖彼此。

客户端保留每日 04:00 的 `ui_am4` 入口，服务端规则任务也改为每日 04:00。计划时间使用安装器配置的 Asia/Shanghai 时区。规则刷新不从 cron 任务内部停止 cron。

只下载到指定目录，不修改系统服务的命令为：

```bash
bash resource/client/ui-script/ui-updateRules --directory /tmp/de-gwd-rules --data-only
```

## 验证与边界

```bash
python3 tests/test_rule_updates.py
python3 tests/test_regressions.py
```

新增规则测试覆盖下载/校验失败保留、格式检查、互斥、路由更新、Pi-hole 新增源及用户设置保留、归档数据一致性和核心二进制不变。

2026-10-17 至 18 日，已在电脑本地 Debian 11 ARM64 VM 重新运行规则和安装器回归，
并实际执行 ARM64 安装包内 Xray 1.4.2、V2Ray 4.41.1 的配置检查，确认所需分类可以加载。
amd64 核心加载也曾通过此前的实际配置检查。

实际部署的本地客户端已验证 Pi-hole 正常 UDP/TCP DNS、doubleclick.net 的 UDP/TCP
阻断及 LAN 查询 gravity 日志；真实透明代理及冷启动后的 23 项检查通过。
实机 gravity 随上游刷新产生的条数可能不同于本仓库 2026-10-16 静态快照，
不能将两者当作同一份数据。详见 [实际部署验证](bugfixes.md)。
