<?php
require_once __DIR__ . '/_bootstrap.php';
header('Cache-Control: no-store');
header('Content-Type: application/json');
echo json_encode(['ready' => ($_SESSION['console_grant']['expires'] ?? 0) > time()]);
