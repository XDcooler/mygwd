<?php
require_once __DIR__ . '/_bootstrap.php';
$kind = $_GET['kind'] ?? '';
if (!is_string($kind) || !in_array($kind, ['config', 'bitwarden'], true)
    || ($_SESSION['backup_grant'][$kind] ?? 0) < time()) {
    http_response_code(403);
    exit('备份下载已过期，请重新生成。');
}
unset($_SESSION['backup_grant'][$kind]);
session_write_close();
$process = proc_open(['sudo','-n','/opt/de_GWD/ui-panel'],
    [0 => ['pipe','r'], 1 => ['pipe','w'], 2 => ['file','/dev/null','w']], $pipes);
if (!is_resource($process)) {
    http_response_code(503);
    exit('备份下载暂不可用。');
}
fwrite($pipes[0], json_encode(['action'=>'downloadBackup','params'=>['kind'=>$kind]]));
fclose($pipes[0]);
// Buffer the first bytes before sending attachment headers, so helper failure
// remains a visible non-2xx response instead of an empty successful download.
$first = fread($pipes[1], 8192);
if ($first === false || $first === '') {
    fclose($pipes[1]);
    proc_close($process);
    http_response_code(502);
    exit('备份已过期或读取失败。');
}
header('Cache-Control: no-store, private');
header('Content-Type: application/octet-stream');
header('Content-Disposition: attachment; filename="' . ($kind === 'config' ? 'de_GWD_bak' : 'Bitwarden_bak.zip') . '"');
echo $first;
fpassthru($pipes[1]);
fclose($pipes[1]);
$exit = proc_close($process);
error_log(json_encode(['component'=>'panel','action'=>'downloadBackup','exit'=>$exit]));
