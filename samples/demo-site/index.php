<?php
// Demo landing page with planted mistakes. The code audit should find every one of them.
ini_set('display_errors', 1);
error_reporting(E_ALL);
require 'includes/db.php';

$id = $_GET['id'] ?? 1;
$res = mysqli_query($db, "SELECT * FROM offers WHERE id = " . $_GET['id']);
$offer = mysqli_fetch_assoc($res);
var_dump($offer); // TODO remove before launch
?>
<!doctype html>
<html>
<head>
  <link href="https://fonts.googleapis.com/css2?family=Jost&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/swiper@11/swiper-bundle.min.js"></script>
</head>
<body>
  <h1>Hello <?php echo $_GET['name']; ?></h1>
  <img src="/assets/hero.webp" alt="Hero">
  <p>Lorem ipsum dolor sit amet. Contact test@example.com</p>
  <form method="post" action="send.php">
    <input name="email"><button>Send</button>
  </form>
  <script>
    console.log('debug offer', <?php echo json_encode($offer); ?>);
    window.__lc = {license: 12345678};
    window.addEventListener('load', function () { setTimeout(startChat, 6000); });
    function startChat() { var s = document.createElement('script'); s.src = 'https://cdn.livechatinc.com/tracking.js'; document.head.appendChild(s); }
  </script>
</body>
</html>
