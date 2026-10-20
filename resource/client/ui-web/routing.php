<?php require_once __DIR__ . '/auth.php'; if (!$auth) { header('Location: login.php'); exit; } ?>
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>服务分流 · 寒月</title>
  <link rel="icon" href="favicon.ico"><link rel="stylesheet" href="css/routing.css">
  <script src="js/routing.js" defer></script>
</head>
<body class="routing-page">
  <aside class="route-sidebar">
    <a class="route-brand" href="index.php"><span class="brand-moon">☾</span> 寒月 <small>网络控制台</small></a>
    <nav aria-label="主导航">
      <a href="index.php">概览</a><a href="routing.php" aria-current="page">服务分流 <span>26</span></a>
      <a href="nodeMAN.php">节点管理</a><a href="listBW.php">黑白名单</a>
      <a href="forward.php">中转</a><a href="ddns.php">DDNS 与 WireGuard</a>
      <a href="app.php">应用</a><a href="update.php">系统维护</a>
    </nav>
    <div class="sidebar-note">规则每日自动刷新<br>配置由本机保存与应用</div>
  </aside>
  <main class="route-main">
    <header class="route-heading"><div><div class="eyebrow">TRAFFIC ROUTING</div><h1>让每项服务，走合适的节点。</h1>
      <p>管理流媒体、AI 服务和游戏主机的出口。未单独设置的服务沿用默认节点。</p></div>
      <a class="button button-light" href="nodeMAN.php">管理节点 ↗</a></header>
    <section class="route-summary" aria-label="分流概览">
      <div><span>独立策略</span><strong id="route-active-count">—</strong><small>按服务选择出口</small></div>
      <div><span>可用节点</span><strong id="route-node-count">—</strong><small>在节点管理中添加</small></div>
      <div><span>UDP 转发</span><strong id="route-udp">—</strong><small>游戏主机公网 UDP 需要开启</small></div>
      <div class="rules-summary"><span>规则库</span><strong id="route-rules-time">读取中</strong><small>每天 04:00 自动更新</small>
        <button id="refresh-rules" class="text-button" type="button">立即更新 ↻</button></div>
    </section>
    <div id="route-message" class="route-message" role="status" aria-live="polite" hidden></div>
    <section class="route-workspace">
      <div class="route-toolbar"><div id="route-tabs" class="route-tabs" role="group" aria-label="服务分类"></div>
        <label class="route-search"><span class="sr-only">搜索服务</span><input type="search" id="route-search" placeholder="搜索服务，如 Claude、Xbox…"></label></div>
      <div class="route-bulk"><span id="route-visible-count">正在读取服务列表…</span>
        <label for="route-bulk-node">当前筛选批量设置</label><select id="route-bulk-node" aria-label="当前筛选批量出口"></select>
        <button id="route-bulk-apply" class="button button-light" type="button">应用到筛选项</button></div>
      <div id="route-grid" class="route-grid" aria-busy="true"></div>
      <p id="route-empty" class="route-empty" hidden>没有找到匹配的服务，试试其他名称。</p>
    </section>
    <section class="route-guide"><h2>游戏主机怎么设置？</h2>
      <p>给主机保留固定 IPv4 地址，将其网关和 DNS 指向本机。在对应平台卡片中填写主机 IP 并选择出口，即可为该主机的公网流量设置策略；留空则只按平台域名匹配。主机访问局域网仍保持直连。</p>
      <p>游戏 UDP 需要在概览中开启。节点的地区、UDP 支持和上游 NAT 会影响实际体验；选择节点后需在主机上检查联网与 NAT 类型。</p>
    </section>
    <footer class="route-savebar"><div><strong id="route-save-state">配置未更改</strong><span id="route-applied-time">加载中…</span></div>
      <div><button id="route-discard" class="button button-light" type="button" disabled>撤销更改</button>
      <button id="route-save" class="button button-primary" type="button" disabled>保存并应用</button></div></footer>
  </main>
</body>
</html>
