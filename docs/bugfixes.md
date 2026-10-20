# 项目分析与本次修复

此项目是面向 Debian 11 的系统安装工具与管理面板，没有独立的 npm/Composer 应用构建流程。核心关系如下：

| 组件 | 作用 |
| --- | --- |
| `client`、`server` | 交互式安装、升级，以及 Nginx、PHP 7.4、DNS、代理、systemd 和网络配置 |
| `resource/client/ui-web/*.php` | 登录和管理页面，读取主配置及系统状态 |
| `resource/client/ui-web/act/*.php` | AJAX 接口，保存配置并调用系统辅助命令 |
| `resource/client/ui-script/*` | Bash 辅助脚本，读取 `/opt/de_GWD/0conf`，管理相关服务 |
| `resource/client/Archive.zip` | `client` 的 `installWebUI` 实际部署的 UI 与辅助脚本归档 |
| `/opt/de_GWD/0conf` | 认证密码哈希、节点、DNS、转发、WireGuard 和应用配置 |

修复包括认证、接口、参数传递、透明代理规则，以及 Debian 11 实际安装中发现的故障。

| 问题 | 修复后的行为 |
| --- | --- |
| `auth.php` 将 cookie 赋值给密码哈希，任意非空 cookie 可绕过登录 | 使用常量时间比较验证带过期时间的 HMAC cookie；cookie 不再包含密码哈希；校验格式、有效期和配置 |
| cookie 路径包含 `; samesite=none`，续期产生 PHP 警告；会话未更新 | 使用 cookie options 设置 `/`、HttpOnly、SameSite=Strict 和 HTTPS 下的 Secure；登录更新 session ID；注销清理 session 与 cookie |
| 登录页面将密码放进 GET URL | 页面改用 POST；服务端保留旧客户端 GET 登录兼容 |
| 诊断接口没有鉴权，其他接口匿名访问只返回空的成功响应 | 所有 action 通过 `_bootstrap.php` 统一鉴权，匿名请求返回 403；路径由 `__DIR__` 解析 |
| 节点、测速、WireGuard、NFS 和升级接口直接拼接 shell 参数 | 索引校验类型及配置范围，NFS/升级字符串校验并通过 shell 参数转义传递，非法请求返回 400 |
| `installed`、`active`、`success`、`block`、`on` 等被当成未定义 PHP 常量 | 明确使用字符串，消除 PHP 7.4 警告及对应 PHP 8 致命错误；这不代表完整 PHP 8 迁移已验证 |
| 使用 `sudo [[ ... ]]` 检查文件，但 `[[` 是 shell 关键字 | 使用 PHP `file_exists` 检查；DDNS 状态查询同步返回脚本输出 |
| uptime 对浮点数作严格整数比较，显示无意义的零天/零小时 | 使用整数分钟拆分天、小时、分钟；处理读取失败；验证 0、59、60、3599、3600、86400、90061 秒边界 |
| NFS 卸载把路径当 sed 正则，可能删除名字相似的其他挂载 | 按配置第一列进行字面值精确匹配，保留其他挂载 |
| 升级 URL 始终删掉最后一个字符，下载与设置并行执行 | 提取完整 HTTPS URL，先下载到临时文件，成功才替换脚本、保存设置并启动辅助命令；下载失败保留原脚本和配置 |
| 恢复接口先异步删除备份，即使上传失败也执行恢复 | 校验文件名、上传状态及来源，移动成功后才运行恢复；无效上传保留原备份；Bitwarden 备份删除与压缩按顺序执行 |
| FileRun 安装函数在下载失败时使用循环外 `break`，仍继续后续操作 | 使用 `return 1` 中止安装，回归验证失败路径不写入 Nginx 配置 |
| 安装命令将依赖数组重新拆成字符串 | 直接传递带引号的数组参数，空数组不启动安装；保留黑白名单脚本原本的字面匹配意图 |
| 已修复散列源码不会自动进入实际安装归档 | 重建 `Archive.zip`，增加可重复构建脚本及所有归档条目与源码的内容一致性检查 |

旧版持久登录 cookie 需要重新登录才能换成新的签名格式。API 的匿名、无效请求现在会返回明确的失败状态，正常登录后的现有 AJAX 调用保持兼容。

## 验证

`python3 tests/test_regressions.py` 包含 19 个测试方法，其中参数及全部 action 接口使用子测试覆盖；直接启动真实 PHP 页面，系统命令由隔离 fixture 内的适配器替代。测试覆盖认证、命令注入、cookie 有效期、上传与下载失败、状态查询、页面渲染、Bash 失败路径、APT 失败中止、NFS 精确匹配和安装归档一致性。测试说明见 [tests/README.md](../tests/README.md)。

另检查安装器、辅助 Bash 脚本和全部 PHP 文件的语法，以及 ShellCheck error 级检查。原有 14 项 ShellCheck error 级结果已清除；这不表示所有 warning/info 级问题均已修复。

修复前的提交在隔离测试中重现了伪造 cookie 获得页面访问和 NFS 删除错误挂载的问题；修复后的对应测试通过。功能测试的 7 个管理页面没有 PHP 警告或致命错误。

## 2026-10-18 Debian 11 实际部署修复

| 安装问题 | 修复后的行为 |
| --- | --- |
| Bullseye 旧镜像、第三方 Nginx 和 DoH 下载入口失效 | Bullseye 使用 Debian 官方 HTTPS 归档源并保留软件包签名校验；Nginx 使用 Debian 的 nginx-full；DoH 使用随仓库提供的两架构构建资产 |
| APT 失败后仍继续配置服务 | 依赖安装和调用链传播失败；回归覆盖 server 的 update/upgrade/install 及 client 的 update/install 五个失败场景 |
| 已存在的受限目录阻止 www-data 启动核心 | 明确创建权限为 0755 的服务目录 |
| Web ZIP 按压缩后大小判断，跳过有效安装包 | 校验 ZIP 完整性和必需文件后再提取，归档内容继续与源码逐项比较 |
| Pi-hole 首次安装与 v2dns 同时占用 53 端口 | 仅安装引导阶段将 FTL 放到 1053；完成后 v2dns 使用 5350，FTL 恢复 53 并删除临时配置 |
| ACME 在不同 HOME 环境安装到错误目录 | 使用官方 ACME 脚本并显式指定 /root/.acme.sh；证书及服务重启失败向上返回；私钥权限为 root:www-data 0640 |
| 本地面板证书未选用 SAN 扩展 | 自签证书包含 localhost 和 127.0.0.1 SAN，可通过固定公开证书进行严格 HTTPS 校验 |
| 版本入口的 404 被当作新版本 | 使用当前仓库地址、超时和版本格式校验；请求失败不安排自动代码更新；面板版本查询失败时回退到本地版本 |
| iptables 的 TPROXY 检查输出混入 UDP 状态 | 状态脚本屏蔽底层规则输出，仅返回 success 或 outline-secondary；回归覆盖带额外输出的规则检查 |

DoH 资产由官方 v2.3.10 的固定提交构建，构建脚本验证 Go 归档 SHA-256、源码提交和模块完整性。
构建来源及两架构产物散列见 [doh-server-build.json](../resource/doh-server-build.json)，
重建命令为本地 Linux VM 中的 `python3 scripts/build-doh-server.py`；保留上游 MIT 许可。

本次已实际部署 Debian 11 amd64 公网服务端和电脑本地的 Debian 11 ARM64 客户端。
服务端取得有效公网 TLS 证书，HTTPS、DoH 和真实 VLESS+WebSocket+TLS 节点连接均已验证。
本地 VM 的真实节点集成测试 23 项通过，并在冷启动后再次通过：透明 TCP/UDP 出口、
中国与私网直连、正常 DNS、UDP/TCP 广告域名阻断及 gravity 日志、UDP 开关、
服务重启和外部防火墙规则保留。辅助 HTTP/UDP 后端仅用于观测直连，不提供替代代理节点。
TCP 与 UDP 的直连出口分别测量，允许上游网络对不同传输使用不同出口。

客户端七项服务已在冷启动后验证自动启动；本地面板 HTTPS 页面使用固定叶证书校验成功。
发布前隔离回归共 62 个不同测试方法通过：PHP/接口及安装流程 19、规则更新 14、
透明代理脚本 11、Pi-hole 安装器 18（含单独以普通用户补跑的 root 权限拒绝检查）。
最终静态检查通过 95 个 Bash、94 个 PHP 和 9 个 Python 文件，两个 DoH 资产散列相符。
随后使用单独授权的面板凭据，真实 HTTPS 集成共 26 项全部通过，补齐匿名读写拒绝、
错误密码拒绝、真实登录、Secure/HttpOnly/SameSite Cookie、七个页面、
网页 UDP 开关与实际 TCP/UDP 流量及页面指示器一致性、服务重启后会话和注销检查。
测试结束后已删除 VM 内临时密码副本。
WireGuard、可选应用、内核更新、IPv6 透明代理和所有配置组合未作端到端验证。
这些结果不等于完整安全审计。真实节点和登录凭据不随仓库发布。
