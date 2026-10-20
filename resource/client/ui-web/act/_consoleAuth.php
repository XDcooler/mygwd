<?php
require_once __DIR__ . '/../auth.php';
header('Cache-Control: no-store');
$grant = $_SESSION['console_grant'] ?? [];
$token = $_COOKIE['gwdconsole'] ?? '';
if (!$auth || ($grant['expires'] ?? 0) < time() || !is_string($token)
    || !isset($grant['token']) || !hash_equals($grant['token'], $token)) {
    http_response_code(403);
    exit;
}
$uri = $_SERVER['HTTP_X_GWD_ORIGINAL_URI'] ?? '';
if (preg_match('~/ttyd/ws(?:\?|$)~', $uri)) {
    if (!empty($grant['used'])) {
        http_response_code(403);
        exit;
    }
    $_SESSION['console_grant']['used'] = true;
}
http_response_code(204);
