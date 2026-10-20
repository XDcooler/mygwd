<?php
require_once __DIR__ . '/auth.php';
if (!$auth) {
    http_response_code(403);
    exit('请先登录面板。');
}
header('Cache-Control: no-store');
?>
<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>管理控制台</title>
<p id="status">正在等待管理服务启动……</p>
<script>
(async function () {
  for (let attempt = 0; attempt < 30; attempt++) {
    try {
      const response = await fetch('act/consoleStatus.php', {cache:'no-store', credentials:'same-origin'});
      if (!response.ok) throw new Error('会话已失效');
      if ((await response.json()).ready) { location.replace('/ttyd/'); return; }
    } catch (error) { document.getElementById('status').textContent = error.message; return; }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  document.getElementById('status').textContent = '控制台未能启动，请返回面板查看操作错误和系统日志。';
})();
</script></html>
