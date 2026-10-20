<?php
require_once __DIR__ . '/_panel.php';
require_once __DIR__ . '/_loginLimit.php';
$secureCookie = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
$cookieOptions = [
    'path' => '/',
    'secure' => $secureCookie,
    'httponly' => true,
    'samesite' => 'Strict',
];
session_set_cookie_params($cookieOptions);
session_start();
if (!isset($_SESSION['service_csrf'])) {
    $_SESSION['service_csrf'] = bin2hex(random_bytes(32));
}

try {
    $config = gwdReadConfig();
} catch (Throwable $error) {
    http_response_code(503);
    exit('管理配置不可用，请检查 root 管理入口及 sudo 配置。');
}
$GWDpwhash = isset($config->address->PWD) ? $config->address->PWD : null;
$hasPassword = is_string($GWDpwhash) && preg_match('/^[a-f0-9]{64}$/D', $GWDpwhash) === 1
    && $GWDpwhash !== str_repeat('0', 64); // The installer uses zeroes for an unset password.
$auth = false;

function gwdClearLoginCookie($options)
{
    setcookie('persistentlogin', '', array_merge($options, ['expires' => time() - 3600]));
}

function gwdLoginCookie($passwordHash, $expires)
{
    return $expires . ':' . hash_hmac('sha256', 'persistentlogin:' . $expires, $passwordHash);
}

if (isset($_GET['gwdpw'])) {
    header('Allow: POST');
    http_response_code(405);
    exit('Use POST to log in.');
}
$loginPassword = $_POST['gwdpw'] ?? null;
if ($loginPassword !== null) {
    $limit = gwdLoginLimitOpen();
    if ($hasPassword && is_string($loginPassword)) {
        $loginHash = hash('sha256', hash('sha256', $loginPassword));
        $auth = hash_equals($GWDpwhash, $loginHash);
    }
    if ($auth) {
        session_regenerate_id(true);
        $_SESSION['GWDhash'] = $GWDpwhash;
        $expires = time() + 60 * 60 * 24 * 7;
        setcookie('persistentlogin', gwdLoginCookie($GWDpwhash, $expires),
            array_merge($cookieOptions, ['expires' => $expires]));
    } else {
        unset($_SESSION['GWDhash']);
        gwdClearLoginCookie($cookieOptions);
        http_response_code(401);
    }
    gwdLoginLimitClose($limit, $auth);
} elseif ($hasPassword) {
    $sessionHash = $_SESSION['GWDhash'] ?? null;
    if (is_string($sessionHash) && hash_equals($GWDpwhash, $sessionHash)) {
        $auth = true;
    } else {
        unset($_SESSION['GWDhash']);
    }

    $cookie = $_COOKIE['persistentlogin'] ?? null;
    if (is_string($cookie) && preg_match('/^([0-9]{10}):[a-f0-9]{64}$/D', $cookie, $matches) === 1
        && (int) $matches[1] > time() && (int) $matches[1] <= time() + 60 * 60 * 24 * 7
        && hash_equals(gwdLoginCookie($GWDpwhash, $matches[1]), $cookie)) {
        if (!$auth) {
            session_regenerate_id(true);
            $_SESSION['GWDhash'] = $GWDpwhash;
        }
        $auth = true;
    } elseif ($cookie !== null) {
        gwdClearLoginCookie($cookieOptions);
    }
} else {
    unset($_SESSION['GWDhash']);
    gwdClearLoginCookie($cookieOptions);
}

if (isset($_GET['logout']) || isset($_POST['logout'])) {
    $token = $_SERVER['HTTP_X_GWD_CSRF'] ?? '';
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST' || !$auth || !is_string($token)
        || !hash_equals($_SESSION['service_csrf'], $token)) {
        http_response_code(403);
        exit('Invalid logout request.');
    }
    if (isset($_SESSION['console_grant'])) {
        try { gwdBroker('installZ'); } catch (Throwable $error) {
            http_response_code(502);
            exit('控制台停止失败，请检查管理服务后再注销。');
        }
    }
    $_SESSION = [];
    session_destroy();
    gwdClearLoginCookie($cookieOptions);
    setcookie('gwdconsole', '', ['expires'=>time()-3600,'path'=>'/ttyd/','secure'=>true,'httponly'=>true,'samesite'=>'Strict']);
    setcookie(session_name(), '', array_merge($cookieOptions, ['expires' => time() - 3600]));
    exit();
}
