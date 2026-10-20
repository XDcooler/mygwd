<?php
require_once __DIR__ . '/_bootstrap.php';
ob_start('ob_gzhandler');

function gwdFormatUptime($seconds)
{
    $minutes = (int) floor(max(0, $seconds) / 60);
    $days = intdiv($minutes, 1440);
    $hours = intdiv($minutes % 1440, 60);
    $result = $days > 0 ? $days . '天' : '';
    $result .= $hours > 0 ? $hours . '小时' : '';
    return $result . ($minutes % 60) . '分钟';
}

$uptime = @file_get_contents('/proc/uptime');
if ($uptime === false) {
    http_response_code(503);
    exit('Uptime unavailable');
}
echo gwdFormatUptime((float) explode(' ', trim($uptime))[0]);
ob_end_flush();
