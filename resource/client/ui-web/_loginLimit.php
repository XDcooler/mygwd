<?php
// Per-peer state, held under flock across password verification. Never use XFF.
function gwdLoginLimitOpen()
{
    $directory = '/var/lib/de-gwd-auth';
    $peer = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
    $path = $directory . '/' . hash('sha256', $peer) . '.json';
    $handle = @fopen($path, 'c+');
    if ($handle === false || !flock($handle, LOCK_EX)) {
        http_response_code(503);
        exit('登录保护服务不可用，请检查部署权限。');
    }
    chmod($path, 0600);
    $state = json_decode(stream_get_contents($handle), true) ?: ['failures'=>0, 'until'=>0, 'last'=>0];
    if (($state['last'] ?? 0) < time() - 900) {
        $state = ['failures'=>0, 'until'=>0, 'last'=>0];
    }
    if (($state['until'] ?? 0) > time()) {
        header('Retry-After: ' . ($state['until'] - time()));
        http_response_code(429);
        flock($handle, LOCK_UN);
        fclose($handle);
        exit('登录尝试过于频繁，请稍后重试。');
    }
    return [$handle, $state];
}

function gwdLoginLimitClose($entry, $success)
{
    [$handle, $state] = $entry;
    $failures = $success ? 0 : min(20, ($state['failures'] ?? 0) + 1);
    $delay = $failures >= 5 ? min(300, 2 ** ($failures - 5)) : 0;
    rewind($handle);
    ftruncate($handle, 0);
    fwrite($handle, json_encode(['failures'=>$failures, 'last'=>time(), 'until'=>time()+$delay]));
    fflush($handle);
    flock($handle, LOCK_UN);
    fclose($handle);
}
