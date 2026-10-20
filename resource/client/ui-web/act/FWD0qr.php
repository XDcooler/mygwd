<?php require_once(__DIR__ . '/_bootstrap.php'); ?>
<?php if (isset($auth) && $auth) {?>
<?php
$FWD0index = gwdRequestIndex('FWD0index', count($config->FORWARD->FWD0->uuid ?? []), 1);

$de_GWDconf = gwdReadConfig();
$serverDomain = $de_GWDconf->address->serverName;
$FWD0port = $de_GWDconf->FORWARD->FWD0->port;
$FWD0uuid = $de_GWDconf->FORWARD->FWD0->uuid[$FWD0index-1]->FWD0uuid;

print("vless://$FWD0uuid@$serverDomain:$FWD0port?encryption=none&security=xtls&flow=xtls-rprx-direct&sni=$serverDomain&type=tcp&tfo=1");
?>
<?php }?>
