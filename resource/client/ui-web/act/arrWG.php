<?php require_once(__DIR__ . '/_bootstrap.php'); ?>
<?php if (isset($auth) && $auth) {?>
<?php
echo json_encode(gwdReadConfig()->wireguard);
?>
<?php }?>
