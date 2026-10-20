<?php
require_once __DIR__ . '/_bootstrap.php';
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

function serviceReply($status, $data) {
    http_response_code($status);
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

if (!isset($_SESSION['service_csrf'])) {
    $_SESSION['service_csrf'] = bin2hex(random_bytes(32));
}
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
if ($method === 'GET') {
    $command = ['sudo', '-n', '/opt/de_GWD/ui-serviceRoutes', 'status'];
    $input = null;
} elseif ($method === 'POST') {
    $token = $_SERVER['HTTP_X_GWD_CSRF'] ?? '';
    if (!is_string($token) || !hash_equals($_SESSION['service_csrf'], $token)) {
        serviceReply(403, ['error' => '会话校验失败，请刷新页面后重试。']);
    }
    if ((int)($_SERVER['CONTENT_LENGTH'] ?? 0) > 98304) {
        serviceReply(413, ['error' => '配置内容过大。']);
    }
    $payload = json_decode(file_get_contents('php://input', false, null, 0, 98305), true);
    if (!is_array($payload)) {
        serviceReply(400, ['error' => '配置格式无效。']);
    }
    if (($payload['action'] ?? '') === 'refreshRules') {
        $command = ['sudo', '-n', '/opt/de_GWD/ui-refreshRules'];
        $input = null;
    } elseif (($payload['action'] ?? '') === 'save' && isset($payload['policies']) && is_array($payload['policies'])) {
        $command = ['sudo', '-n', '/opt/de_GWD/ui-serviceRoutes', 'apply'];
        $input = json_encode($payload['policies']);
    } else {
        serviceReply(400, ['error' => '不支持的操作。']);
    }
} else {
    header('Allow: GET, POST');
    serviceReply(405, ['error' => '不支持的请求方法。']);
}
$csrf = $_SESSION['service_csrf'];
session_write_close();
$pipes = [];
$process = proc_open($command, [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
if (!is_resource($process)) {
    serviceReply(503, ['error' => '分流服务暂不可用。']);
}
if ($input !== null) {
    fwrite($pipes[0], $input);
}
fclose($pipes[0]);
$output = stream_get_contents($pipes[1]);
fclose($pipes[1]);
stream_get_contents($pipes[2]); // Never return command errors or private configuration.
fclose($pipes[2]);
$code = proc_close($process);
if ($command[2] === '/opt/de_GWD/ui-refreshRules') {
    serviceReply($code === 0 ? 202 : 409, $code === 0
        ? ['started' => true, 'message' => '已开始更新；完成后可查看规则库时间。']
        : ['error' => '更新任务正在运行或暂时无法启动。']);
}
$data = json_decode($output, true);
if ($code !== 0 || !is_array($data)) {
    serviceReply(400, ['error' => '未能应用分流。请检查所选节点、主机 IP 和服务状态，原配置已保留。']);
}
if ($method === 'GET') {
    $data['csrf'] = $csrf;
}
serviceReply(200, $data);
