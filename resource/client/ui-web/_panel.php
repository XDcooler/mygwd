<?php
// Never use a browser-supplied command, URL or file path as a privileged command.
function gwdBroker($action, $params = [])
{
    $pipes = [];
    $process = proc_open(['sudo', '-n', '/opt/de_GWD/ui-panel'],
        [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (!is_resource($process)) {
        throw new RuntimeException('管理服务不可用。');
    }
    $request = json_encode(['action' => $action, 'params' => (object) $params], JSON_THROW_ON_ERROR);
    $sent = 0;
    while ($sent < strlen($request)) {
        $written = fwrite($pipes[0], substr($request, $sent, 65536));
        if ($written === false || $written === 0) {
            fclose($pipes[0]);
            fclose($pipes[1]);
            fclose($pipes[2]);
            proc_close($process);
            throw new RuntimeException('无法提交管理请求。');
        }
        $sent += $written;
    }
    fclose($pipes[0]);
    $output = stream_get_contents($pipes[1]);
    fclose($pipes[1]);
    $errorOutput = stream_get_contents($pipes[2]);
    fclose($pipes[2]);
    $exit = proc_close($process);
    $data = json_decode($output, true);
    error_log(json_encode(['component' => 'panel', 'action' => $action, 'exit' => $exit]));
    if ($exit !== 0 || !is_array($data) || ($data['ok'] ?? false) !== true) {
        // Only the broker's structured public error is exposed. Raw sudo or
        // subprocess stderr may contain private details and is never returned.
        $failure = json_decode($errorOutput, true);
        $detail = is_array($failure) && is_string($failure['error'] ?? null)
            ? substr($failure['error'], 0, 400) : '请检查管理入口权限和系统日志。';
        throw new RuntimeException('管理操作失败（退出码 ' . $exit . '）：' . $detail . ' 服务可能已部分变更。');
    }
    return $data['result'] ?? null;
}

function gwdReadConfig($associative = false)
{
    static $snapshot = null;
    if ($snapshot === null) {
        $snapshot = gwdBroker('snapshot');
        if (!is_array($snapshot)) {
            throw new RuntimeException('无法读取管理配置。');
        }
    }
    return $associative ? $snapshot : json_decode(json_encode($snapshot));
}

function gwdReadAction($action)
{
    try {
        return (string) gwdBroker($action);
    } catch (Throwable $error) {
        return '';
    }
}

function gwdRuntime($target)
{
    try {
        return (string) gwdBroker('runtime', ['target' => $target]);
    } catch (Throwable $error) {
        return '';
    }
}

function gwdManagementAction($action)
{
    header('Cache-Control: no-store');
    if ((int) ($_SERVER['CONTENT_LENGTH'] ?? 0) > 36 * 1024 * 1024) {
        http_response_code(413);
        exit('请求过大。');
    }
    $params = ($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST' ? $_POST : $_GET;
    if (in_array($action, ['restore', 'restoreBitwarden'], true)) {
        $file = $_FILES['file'] ?? null;
        $expected = $action === 'restore' ? 'de_GWD_bak' : 'Bitwarden_bak.zip';
        $limit = $action === 'restore' ? 1048576 : 32 * 1024 * 1024;
        if (!is_array($file) || ($file['name'] ?? '') !== $expected
            || ($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK
            || !is_uploaded_file($file['tmp_name'] ?? '') || ($file['size'] ?? PHP_INT_MAX) > $limit) {
            http_response_code(400);
            exit('备份上传无效或超过大小限制。');
        }
        $params = ['data' => base64_encode(file_get_contents($file['tmp_name']))];
    }
    try {
        $result = gwdBroker($action, $params);
        if ($action === 'updateRun' || $action === 'genCER'
            || in_array($action, ['installDocker','installJellyfin','installNFS','installNetdata',
                'installMariaDB','installFileRun','installBitwarden','installWG'], true)) {
            $_SESSION['console_grant'] = ['expires' => time() + 600,
                'token' => bin2hex(random_bytes(32)), 'used' => false];
            setcookie('gwdconsole', $_SESSION['console_grant']['token'], [
                'expires' => time() + 600, 'path' => '/ttyd/', 'secure' => true,
                'httponly' => true, 'samesite' => 'Strict']);
        }
        if ($action === 'installZ') {
            unset($_SESSION['console_grant']);
        }
        if (in_array($action, ['backup','backupBitwarden'], true)) {
            $_SESSION['backup_grant'][$result] = time() + 300;
        }
        echo is_scalar($result) ? (string) $result : json_encode($result);
    } catch (Throwable $error) {
        http_response_code(502);
        header('Content-Type: text/plain; charset=utf-8');
        echo $error->getMessage();
    }
    exit;
}
