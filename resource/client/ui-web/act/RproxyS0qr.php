<?php require_once(__DIR__ . '/_bootstrap.php'); ?>
<?php if (isset($auth) && $auth) {?>
<?php
$RproxyS0index = gwdRequestIndex('RproxyS0index', count($config->FORWARD->Rproxy->server->inUUID ?? []), 1);

$de_GWDconf = gwdReadConfig();
$serverDomain = $de_GWDconf->address->serverName;
$RproxyS0port = $de_GWDconf->FORWARD->Rproxy->server->tunnel->port;
$RproxyS0uuid = $de_GWDconf->FORWARD->Rproxy->server->inUUID[$RproxyS0index-1]->RproxyS0uuid;

print("vless://$RproxyS0uuid@$serverDomain:$RproxyS0port?encryption=none&security=xtls&flow=xtls-rprx-direct&sni=$serverDomain&type=tcp&tfo=1");
?>
<?php }?>
