<?php
require_once __DIR__ . '/../auth.php';
if (!$auth) {
    http_response_code(403);
    exit('Authentication required');
}

$readOnlyActions = ['FWD0qr', 'FWD1qr', 'RproxyS0qr', 'WGqr', 'arrWG', 'checkCER', 'checkDDNS3322', 'checkDDNScf', 'checkDDNSip', 'checkWG', 'consoleStatus', 'updateStatus', 'downloadBackup', 'getNFSlist', 'pingTCP', 'pingTCPDOH1', 'pingTCPDOH2', 'pingTCPxDNS', 'speedT', 'testBaidu', 'testGoogle', 'uptime'];
$actionName = basename($_SERVER['SCRIPT_FILENAME'] ?? '', '.php');
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
if ($actionName !== 'serviceRoutes' && !in_array($actionName, $readOnlyActions, true)) {
    if ($method !== 'POST') {
        header('Allow: POST');
        http_response_code(405);
        exit('Use POST for management changes.');
    }
    $token = $_SERVER['HTTP_X_GWD_CSRF'] ?? '';
    if (!is_string($token) || !hash_equals($_SESSION['service_csrf'], $token)) {
        http_response_code(403);
        exit('会话校验失败，请刷新后重试。');
    }
} elseif (!in_array($method, ['GET', 'POST'], true)) {
    header('Allow: GET, POST');
    http_response_code(405);
    exit('Unsupported request method');
}
header('Cache-Control: no-store');

function gwdRequestIndex($name, $maximum = 999999999, $minimum = 0)
{
    $value = ($_SERVER['REQUEST_METHOD'] === 'POST' ? $_POST[$name] ?? null : $_GET[$name] ?? null) ?? null;
    if (!is_string($value) || preg_match('/^[0-9]+$/D', $value) !== 1
        || strlen($value) > 9) {
        http_response_code(400);
        exit('Invalid index');
    }
    $index = (int) $value;
    if ($index < $minimum || $index > $maximum) {
        http_response_code(400);
        exit('Index out of range');
    }
    return $index;
}

function gwdRequestNodeIndex($name)
{
    global $config;
    $nodes = $config->v2node ?? [];
    return gwdRequestIndex($name, is_array($nodes) ? count($nodes) - 1 : -1);
}

function gwdClientCount()
{
    global $config;
    $clients = $config->wireguard->clients ?? [];
    return is_array($clients) ? count($clients) : 0;
}

function gwdRequestString($name)
{
    $value = ($_SERVER['REQUEST_METHOD'] === 'POST' ? $_POST[$name] ?? null : $_GET[$name] ?? null) ?? null;
    if (!is_string($value) || $value === '' || preg_match('/[\x00-\x1f\x7f]/', $value)) {
        http_response_code(400);
        exit('Invalid parameter');
    }
    return $value;
}
