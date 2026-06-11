/* VideoMatch フロントエンド
 * 画面遷移: auth → lobby → waiting → consent → call → survey → done → lobby
 */

const API_BASE = ""; // 同一オリジンで配信。別ホストに置く場合はここを変更

// ---- 状態 -------------------------------------------------------------------
let token = localStorage.getItem("vm_token") || "";
let username = localStorage.getItem("vm_username") || "";
let ws = null;
let pc = null;            // RTCPeerConnection
let localStream = null;
let currentRoomId = "";
let isInitiator = false;
let pendingCandidates = []; // remoteDescription設定前に届いたICE候補
let sessionTimer = null;
let sessionSeconds = 600; // 10分 = 600秒
let selectedRole = "";   // ダッシュボードで選んだ役割 ("speaker" | "listener")
let myRole = "";         // 現在のセッションでの役割
let myNickname = "";     // セッション限定のランダムな呼び名
let peerRole = "";
let peerNickname = "";

const RTC_CONFIG = {
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};

// ---- ユーティリティ -----------------------------------------------------------
const $ = (id) => document.getElementById(id);

const roleLabel = (role) => (role === "speaker" ? "話し手" : "聞き手");

function showScreen(name) {
  document.querySelectorAll(".screen").forEach((el) => el.classList.add("hidden"));
  $(`screen-${name}`).classList.remove("hidden");
}

async function api(path, method = "GET", body = null) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(API_BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `エラー (${res.status})`);
  return data;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setLoggedIn(newToken, newUsername) {
  token = newToken;
  username = newUsername;
  localStorage.setItem("vm_token", token);
  localStorage.setItem("vm_username", username);
  $("user-name").textContent = `👤 ${username}`;
  $("user-info").classList.remove("hidden");
  $("dash-username").textContent = username;
  showLobby();
}

/* ロビー(ダッシュボード)を表示し、データを読み込む */
function showLobby() {
  showScreen("lobby");
  loadDashboard();
  ensureWS();
}

async function ensureWS() {
  if (ws && ws.readyState <= WebSocket.OPEN) return;
  try {
    await connectWS();
  } catch (err) {
    $("lobby-error").textContent = err.message;
  }
}

function logout() {
  token = "";
  username = "";
  localStorage.removeItem("vm_token");
  localStorage.removeItem("vm_username");
  cleanupCall();
  if (ws) { ws.close(); ws = null; }
  $("user-info").classList.add("hidden");
  showScreen("auth");
}

// ---- 認証画面 -----------------------------------------------------------------
let authMode = "login";

$("tab-login").onclick = () => setAuthMode("login");
$("tab-register").onclick = () => setAuthMode("register");

function setAuthMode(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("auth-submit").textContent = mode === "login" ? "ログイン" : "登録する";
  $("auth-error").textContent = "";
}

$("auth-form").onsubmit = async (e) => {
  e.preventDefault();
  $("auth-error").textContent = "";
  try {
    const data = await api(`/api/${authMode === "login" ? "login" : "register"}`, "POST", {
      username: $("auth-username").value.trim(),
      password: $("auth-password").value,
    });
    setLoggedIn(data.token, data.username);
  } catch (err) {
    $("auth-error").textContent = err.message;
  }
};

$("btn-logout").onclick = logout;

// ---- WebSocket ----------------------------------------------------------------
function connectWS() {
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onopen = () => resolve();
    ws.onerror = () => reject(new Error("サーバーに接続できません"));
    ws.onmessage = (e) => handleWSMessage(JSON.parse(e.data));
    ws.onclose = () => {
      ws = null;
      // 通話中・待機中に切れた場合はロビーへ戻す
      const active = ["screen-waiting", "screen-consent", "screen-call"].some(
        (id) => !$(id).classList.contains("hidden")
      );
      if (active) {
        cleanupCall();
        $("lobby-error").textContent = "サーバーとの接続が切れました";
        showScreen("lobby");
      }
    };
  });
}

function wsSend(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

async function handleWSMessage(msg) {
  switch (msg.type) {
    case "queued": {
      const opposite = msg.role === "speaker" ? "listener" : "speaker";
      $("waiting-role").textContent = `あなたの役割: ${roleLabel(msg.role)}`;
      $("waiting-note").textContent = `${roleLabel(opposite)}の方が見つかり次第、自動的にマッチングされます。`;
      showScreen("waiting");
      break;
    }

    case "matched":
      currentRoomId = msg.room_id;
      myRole = msg.my_role;
      myNickname = msg.my_nickname;
      peerRole = msg.peer_role;
      peerNickname = msg.peer_nickname;
      $("consent-peer").textContent = peerNickname;
      $("consent-peer-role").textContent = roleLabel(peerRole);
      $("consent-my-name").textContent = myNickname;
      $("consent-my-role").textContent = roleLabel(myRole);
      $("consent-status").textContent = "";
      $("btn-consent-ok").disabled = false;
      $("btn-consent-ng").disabled = false;
      loadFaceLandmarker(); // 同意画面の間にモデルを先読みしておく
      renderAvatarPicker();
      showScreen("consent");
      break;

    case "call_start":
      isInitiator = msg.initiator;
      await startCall();
      break;

    case "peer_declined":
      // 相手が同意しなかった → 同じ役割で自動再マッチング
      cleanupCall(false);
      wsSend({ type: "join_queue", role: myRole || selectedRole });
      break;

    case "signal":
      await handleSignal(msg.data);
      break;

    case "peer_left":
      // 相手が退出 → 通話していたならアンケートへ
      if (!$("screen-call").classList.contains("hidden")) {
        endCallToSurvey(false);
      } else {
        cleanupCall();
        showLobby();
      }
      break;

    case "error":
      $("lobby-error").textContent = msg.message || "エラーが発生しました";
      cleanupCall();
      showScreen("lobby");
      break;
  }
}

// ---- マッチング -----------------------------------------------------------------
function selectRole(role) {
  selectedRole = role;
  $("role-speaker").classList.toggle("selected", role === "speaker");
  $("role-listener").classList.toggle("selected", role === "listener");
  const btn = $("btn-start-matching");
  btn.disabled = false;
  btn.textContent = `${roleLabel(role)}として相手を探す`;
}
$("role-speaker").onclick = () => selectRole("speaker");
$("role-listener").onclick = () => selectRole("listener");

$("btn-start-matching").onclick = async () => {
  $("lobby-error").textContent = "";
  if (!selectedRole) {
    $("lobby-error").textContent = "「話し手」か「聞き手」を選んでください";
    return;
  }
  try {
    if (!ws) await connectWS();
    wsSend({ type: "join_queue", role: selectedRole });
  } catch (err) {
    $("lobby-error").textContent = err.message;
  }
};

$("btn-cancel-matching").onclick = () => {
  wsSend({ type: "cancel_queue" });
  showLobby();
};

// ---- 同意フロー -----------------------------------------------------------------
$("btn-consent-ok").onclick = async () => {
  $("btn-consent-ok").disabled = true;
  $("btn-consent-ng").disabled = true;
  $("consent-status").textContent = "カメラ・マイクを準備しています…";
  try {
    rawStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch (err) {
    $("consent-status").textContent = "";
    $("lobby-error").textContent =
      "カメラ・マイクを利用できませんでした。ブラウザの設定を確認してください。";
    wsSend({ type: "consent", accept: false });
    showLobby();
    return;
  }
  $("consent-status").textContent = "アバターを準備しています…";
  localStream = await buildAvatarStream();
  $("consent-status").textContent = "相手の同意を待っています…";
  wsSend({ type: "consent", accept: true });
};

$("btn-consent-ng").onclick = () => {
  wsSend({ type: "consent", accept: false });
  cleanupCall();
  showLobby();
};

// ---- アバター(MediaPipe顔トラッキング) ------------------------------------
// カメラの実映像は顔の動きの解析だけに使い、ネットワークには一切流さない。
// 相手にはCanvasに描いたアバターを captureStream() で送る。
const MEDIAPIPE_VERSION = "0.10.14";
let faceLandmarker = null;
let landmarkerPromise = null;
let rawStream = null;     // カメラ・マイクの生ストリーム(端末内に閉じる)
let avatarCanvas = null;
let avatarCtx = null;
let avatarLoop = null;
let audioCtxRef = null;
let audioAnalyser = null; // トラッキング不可時のフォールバック(声で口を動かす)

// 表情の現在値と目標値。毎フレーム補間してなめらかに動かす
const faceCur = { x: 0, y: 0, roll: 0, blinkL: 0, blinkR: 0, jaw: 0, smile: 0, brow: 0 };
const faceTgt = { x: 0, y: 0, roll: 0, blinkL: 0, blinkR: 0, jaw: 0, smile: 0, brow: 0 };

function loadFaceLandmarker() {
  if (!landmarkerPromise) {
    landmarkerPromise = (async () => {
      const vision = await import(
        `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MEDIAPIPE_VERSION}/+esm`
      );
      const fileset = await vision.FilesetResolver.forVisionTasks(
        `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MEDIAPIPE_VERSION}/wasm`
      );
      faceLandmarker = await vision.FaceLandmarker.createFromOptions(fileset, {
        baseOptions: {
          modelAssetPath:
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
          delegate: "GPU",
        },
        runningMode: "VIDEO",
        numFaces: 1,
        outputFaceBlendshapes: true,
      });
      return faceLandmarker;
    })().catch((err) => {
      console.warn("顔トラッキングを初期化できませんでした。音声連動にフォールバックします", err);
      landmarkerPromise = null; // 次回の通話で再試行できるようにする
      return null;
    });
  }
  return landmarkerPromise;
}

async function buildAvatarStream() {
  avatarCanvas = document.createElement("canvas");
  avatarCanvas.width = 480;
  avatarCanvas.height = 360;
  avatarCtx = avatarCanvas.getContext("2d");

  // トラッキング用の非表示video。画面にもネットワークにも出さない
  const trackVideo = document.createElement("video");
  trackVideo.muted = true;
  trackVideo.playsInline = true;
  trackVideo.srcObject = new MediaStream(rawStream.getVideoTracks());
  await trackVideo.play().catch(() => {});

  await loadFaceLandmarker();
  if (!faceLandmarker) setupAudioFallback();

  startAvatarLoop(trackVideo);

  const stream = avatarCanvas.captureStream(24);
  rawStream.getAudioTracks().forEach((t) => stream.addTrack(t));
  return stream;
}

function setupAudioFallback() {
  try {
    audioCtxRef = new AudioContext();
    const src = audioCtxRef.createMediaStreamSource(rawStream);
    audioAnalyser = audioCtxRef.createAnalyser();
    audioAnalyser.fftSize = 256;
    src.connect(audioAnalyser);
  } catch { /* 口は動かないが通話自体は続行できる */ }
}

function startAvatarLoop(trackVideo) {
  let lastVideoTime = -1;
  const tick = () => {
    if (faceLandmarker && trackVideo.readyState >= 2 && trackVideo.currentTime !== lastVideoTime) {
      lastVideoTime = trackVideo.currentTime;
      try {
        updateFaceFromResult(faceLandmarker.detectForVideo(trackVideo, performance.now()));
      } catch { /* 一時的な解析失敗は無視 */ }
    }
    if (!faceLandmarker && audioAnalyser) {
      const buf = new Uint8Array(audioAnalyser.frequencyBinCount);
      audioAnalyser.getByteFrequencyData(buf);
      const vol = buf.reduce((a, b) => a + b, 0) / buf.length / 255;
      faceTgt.jaw = Math.min(1, vol * 4);
    }
    for (const k of Object.keys(faceCur)) {
      faceCur[k] += (faceTgt[k] - faceCur[k]) * 0.35;
    }
    drawAvatar();
    avatarLoop = requestAnimationFrame(tick);
  };
  tick();
}

function updateFaceFromResult(res) {
  const lm = res.faceLandmarks && res.faceLandmarks[0];
  if (!lm) return;
  const eyeL = lm[33], eyeR = lm[263], nose = lm[1];
  // ミラー表示(自分が右を向いたらアバターも画面上で右)になるよう左右を反転
  faceTgt.roll = -Math.atan2(eyeR.y - eyeL.y, eyeR.x - eyeL.x);
  faceTgt.x = (0.5 - nose.x) * 160;
  faceTgt.y = (nose.y - 0.5) * 120;
  const bs = {};
  if (res.faceBlendshapes && res.faceBlendshapes[0]) {
    for (const c of res.faceBlendshapes[0].categories) bs[c.categoryName] = c.score;
  }
  faceTgt.blinkL = bs.eyeBlinkRight || 0; // ミラーなので左右を入れ替える
  faceTgt.blinkR = bs.eyeBlinkLeft || 0;
  faceTgt.jaw = bs.jawOpen || 0;
  faceTgt.smile = ((bs.mouthSmileLeft || 0) + (bs.mouthSmileRight || 0)) / 2;
  faceTgt.brow = (bs.browInnerUp || 0) - ((bs.browDownLeft || 0) + (bs.browDownRight || 0)) / 2;
}

// ---- アバターの種類 -----------------------------------------------------------
const AVATARS = {
  maru: { label: "まる" },
  neko: { label: "ねこ" },
  usagi: { label: "うさぎ" },
  kuma: { label: "くま" },
  hito: { label: "ひと" },
};
let avatarType = localStorage.getItem("vm_avatar") || "maru";
if (!AVATARS[avatarType]) avatarType = "maru";

function drawAvatar() {
  drawAvatarOn(avatarCtx, avatarCanvas.width, avatarCanvas.height, avatarType, myRole, faceCur);
}

/* ほっぺ */
function drawCheeks(ctx, color, y = 22) {
  ctx.fillStyle = color;
  for (const sx of [-1, 1]) {
    ctx.beginPath();
    ctx.ellipse(sx * 56, y, 13, 9, 0, 0, Math.PI * 2);
    ctx.fill();
  }
}

/* 目・まゆ・口(全アバター共通。pで配色と位置を切り替える) */
function drawFaceParts(ctx, f, p) {
  for (const [sx, blink] of [[-1, f.blinkL], [1, f.blinkR]]) {
    const ex = sx * 36, ey = p.eyeY;
    if (blink > 0.6) {
      // 閉じ目は弧で描く
      ctx.strokeStyle = p.closedColor;
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.arc(ex, ey, 12, 0.15 * Math.PI, 0.85 * Math.PI);
      ctx.stroke();
    } else if (p.eyeStyle === "sclera") {
      // 白目+瞳
      ctx.fillStyle = p.eyeColor;
      ctx.beginPath();
      ctx.ellipse(ex, ey, 13, 13 * (1 - blink * 0.7), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = p.pupilColor;
      ctx.beginPath();
      ctx.arc(ex, ey + 1, 6, 0, Math.PI * 2);
      ctx.fill();
    } else {
      // 黒目だけ(動物向け)
      ctx.fillStyle = p.pupilColor;
      ctx.beginPath();
      ctx.ellipse(ex, ey, 9, 10 * (1 - blink * 0.7), 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
      ctx.beginPath();
      ctx.arc(ex + 3, ey - 3, 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
    // まゆ(驚くと上がり、ひそめると下がる)
    ctx.strokeStyle = p.browColor;
    ctx.lineWidth = 4;
    ctx.beginPath();
    ctx.moveTo(ex - 12, ey - 24 - f.brow * 10);
    ctx.quadraticCurveTo(ex, ey - 30 - f.brow * 12, ex + 12, ey - 24 - f.brow * 10);
    ctx.stroke();
  }
  // 口(開閉と笑顔に追従)
  const mw = (p.mouthW || 34) + f.smile * 16;
  const open = 3 + f.jaw * (p.openScale || 32);
  const my = p.mouthY;
  ctx.fillStyle = p.mouthColor;
  ctx.beginPath();
  ctx.moveTo(-mw / 2, my - f.smile * 6);
  ctx.quadraticCurveTo(0, my - f.smile * 16, mw / 2, my - f.smile * 6);
  ctx.quadraticCurveTo(0, my + open, -mw / 2, my - f.smile * 6);
  ctx.fill();
}

/* アバター本体。fは表情状態(faceCurと同じ形)。サムネイル描画にも使う */
function drawAvatarOn(ctx, W, H, type, role, f) {
  // 背景(ブランドの生成り色グラデーション)
  const bg = ctx.createLinearGradient(0, 0, 0, H);
  bg.addColorStop(0, "#f8f0e5");
  bg.addColorStop(1, "#e8d5bf");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);

  // 役割の色: 話し手=オレンジ / 聞き手=ネイビー
  const roleMain = role === "speaker" ? "#ee6c4d" : "#3d5a80";
  const roleDark = role === "speaker" ? "#d4553a" : "#2c4460";

  ctx.save();
  ctx.scale(W / 480, H / 360); // 以降は480x360の座標系で描く

  // からだ(頭の動きに少しだけ追従)
  const bodyX = 240 + f.x * 0.4;
  if (type === "hito") {
    // 肩(シャツは役割色)
    ctx.fillStyle = roleMain;
    ctx.beginPath();
    ctx.ellipse(bodyX, 372, 116, 78, 0, Math.PI, 2 * Math.PI);
    ctx.fill();
  } else {
    const bodyColor = { maru: roleDark, neko: "#8e96a3", usagi: "#e7decd", kuma: "#9c7350" }[type];
    ctx.fillStyle = bodyColor;
    ctx.beginPath();
    ctx.ellipse(bodyX, 344, 92, 58, 0, Math.PI, 2 * Math.PI);
    ctx.fill();
    if (type !== "maru") {
      // 動物には役割色のマフラー
      ctx.fillStyle = roleMain;
      ctx.beginPath();
      ctx.ellipse(bodyX, 290, 60, 15, 0, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  ctx.save();
  ctx.translate(240 + f.x, 190 + f.y);
  ctx.rotate(f.roll * 0.7);

  if (type === "maru") {
    ctx.fillStyle = roleMain;
    ctx.beginPath();
    ctx.arc(0, 0, 96, 0, Math.PI * 2);
    ctx.fill();
    // 頭の上の葉っぱ(「聴く力を育てる」モチーフ)
    ctx.strokeStyle = "#588157";
    ctx.lineWidth = 5;
    ctx.beginPath();
    ctx.moveTo(0, -94);
    ctx.quadraticCurveTo(4, -110, 2, -118);
    ctx.stroke();
    ctx.fillStyle = "#588157";
    ctx.beginPath();
    ctx.ellipse(10, -122, 14, 8, -0.5, 0, Math.PI * 2);
    ctx.fill();
    // 顔のパネル(うっすら明るく)
    ctx.fillStyle = "rgba(255, 255, 255, 0.12)";
    ctx.beginPath();
    ctx.ellipse(0, 16, 66, 58, 0, 0, Math.PI * 2);
    ctx.fill();
    drawCheeks(ctx, "rgba(255, 255, 255, 0.25)");
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#27313f",
      browColor: "rgba(255, 255, 255, 0.85)", closedColor: "#fff",
      mouthColor: "#27313f", eyeY: -12, mouthY: 38,
    });
  } else if (type === "neko") {
    // 耳(頭より先に描いて後ろに重ねる)
    for (const sx of [-1, 1]) {
      ctx.fillStyle = "#aab2bf";
      ctx.beginPath();
      ctx.moveTo(sx * 38, -78);
      ctx.lineTo(sx * 88, -118);
      ctx.lineTo(sx * 84, -52);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "#f3b8c0";
      ctx.beginPath();
      ctx.moveTo(sx * 50, -76);
      ctx.lineTo(sx * 78, -100);
      ctx.lineTo(sx * 76, -62);
      ctx.closePath();
      ctx.fill();
    }
    ctx.fillStyle = "#aab2bf";
    ctx.beginPath();
    ctx.arc(0, 0, 92, 0, Math.PI * 2);
    ctx.fill();
    // 口元の白いマズル
    ctx.fillStyle = "#f4f1ea";
    ctx.beginPath();
    ctx.ellipse(0, 36, 44, 32, 0, 0, Math.PI * 2);
    ctx.fill();
    // 鼻
    ctx.fillStyle = "#e88a96";
    ctx.beginPath();
    ctx.moveTo(-7, 18);
    ctx.lineTo(7, 18);
    ctx.lineTo(0, 28);
    ctx.closePath();
    ctx.fill();
    // ひげ
    ctx.strokeStyle = "rgba(70, 80, 95, 0.55)";
    ctx.lineWidth = 2;
    for (const sx of [-1, 1]) {
      for (const [dy, tilt] of [[-2, -6], [8, 0], [18, 6]]) {
        ctx.beginPath();
        ctx.moveTo(sx * 48, 28 + dy);
        ctx.lineTo(sx * 96, 22 + dy + tilt);
        ctx.stroke();
      }
    }
    drawCheeks(ctx, "rgba(238, 108, 77, 0.22)");
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#3a4250",
      browColor: "rgba(70, 80, 95, 0.7)", closedColor: "#3a4250",
      mouthColor: "#7a4a52", eyeY: -16, mouthY: 42, mouthW: 26, openScale: 24,
    });
  } else if (type === "usagi") {
    // 長い耳
    for (const sx of [-1, 1]) {
      ctx.save();
      ctx.translate(sx * 40, -84);
      ctx.rotate(sx * 0.18);
      ctx.fillStyle = "#f6f0e4";
      ctx.beginPath();
      ctx.ellipse(0, -52, 24, 64, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#f3b8c0";
      ctx.beginPath();
      ctx.ellipse(0, -48, 12, 44, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
    ctx.fillStyle = "#f6f0e4";
    ctx.beginPath();
    ctx.arc(0, 0, 92, 0, Math.PI * 2);
    ctx.fill();
    // 鼻
    ctx.fillStyle = "#e88a96";
    ctx.beginPath();
    ctx.moveTo(-6, 20);
    ctx.lineTo(6, 20);
    ctx.lineTo(0, 29);
    ctx.closePath();
    ctx.fill();
    drawCheeks(ctx, "rgba(238, 108, 77, 0.25)");
    drawFaceParts(ctx, f, {
      eyeStyle: "dot", pupilColor: "#4a3f38",
      browColor: "rgba(120, 100, 85, 0.7)", closedColor: "#4a3f38",
      mouthColor: "#7a4a52", eyeY: -14, mouthY: 40, mouthW: 24, openScale: 22,
    });
  } else if (type === "kuma") {
    // 丸い耳
    for (const sx of [-1, 1]) {
      ctx.fillStyle = "#b58a64";
      ctx.beginPath();
      ctx.arc(sx * 64, -72, 30, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#8a6347";
      ctx.beginPath();
      ctx.arc(sx * 64, -72, 16, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = "#b58a64";
    ctx.beginPath();
    ctx.arc(0, 0, 92, 0, Math.PI * 2);
    ctx.fill();
    // マズル
    ctx.fillStyle = "#e8d3b8";
    ctx.beginPath();
    ctx.ellipse(0, 34, 42, 32, 0, 0, Math.PI * 2);
    ctx.fill();
    // 鼻
    ctx.fillStyle = "#5d4534";
    ctx.beginPath();
    ctx.ellipse(0, 20, 10, 7, 0, 0, Math.PI * 2);
    ctx.fill();
    drawCheeks(ctx, "rgba(238, 108, 77, 0.2)");
    drawFaceParts(ctx, f, {
      eyeStyle: "dot", pupilColor: "#3f3128",
      browColor: "rgba(90, 70, 52, 0.8)", closedColor: "#3f3128",
      mouthColor: "#6b4b38", eyeY: -16, mouthY: 42, mouthW: 26, openScale: 24,
    });
  } else if (type === "hito") {
    // 首
    ctx.fillStyle = "#f0c8a2";
    ctx.fillRect(-22, 70, 44, 48);
    // 耳
    for (const sx of [-1, 1]) {
      ctx.fillStyle = "#f0c8a2";
      ctx.beginPath();
      ctx.arc(sx * 86, 4, 16, 0, Math.PI * 2);
      ctx.fill();
    }
    // 顔(少し縦長)
    ctx.fillStyle = "#f4cda6";
    ctx.beginPath();
    ctx.ellipse(0, 0, 88, 96, 0, 0, Math.PI * 2);
    ctx.fill();
    // 髪(前髪つきショート)
    ctx.fillStyle = "#4d3a2c";
    ctx.beginPath();
    ctx.moveTo(-90, -2);
    ctx.quadraticCurveTo(-96, -60, -52, -86);
    ctx.quadraticCurveTo(0, -108, 52, -86);
    ctx.quadraticCurveTo(96, -60, 90, -2);
    ctx.quadraticCurveTo(74, -36, 54, -44);
    ctx.quadraticCurveTo(36, -28, 18, -46);
    ctx.quadraticCurveTo(0, -30, -18, -46);
    ctx.quadraticCurveTo(-36, -28, -54, -44);
    ctx.quadraticCurveTo(-74, -36, -90, -2);
    ctx.closePath();
    ctx.fill();
    drawCheeks(ctx, "rgba(238, 108, 77, 0.18)", 26);
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#3a3026",
      browColor: "#4d3a2c", closedColor: "#3a3026",
      mouthColor: "#a8493f", eyeY: -6, mouthY: 44,
    });
  }

  ctx.restore();
  ctx.restore();
}

/* 同意画面のアバター選択サムネイルを描画する */
function renderAvatarPicker() {
  const list = $("avatar-list");
  list.innerHTML = "";
  const preview = { x: 0, y: 0, roll: 0, blinkL: 0, blinkR: 0, jaw: 0.15, smile: 0.45, brow: 0.1 };
  for (const [type, def] of Object.entries(AVATARS)) {
    const opt = document.createElement("button");
    opt.type = "button";
    opt.className = "avatar-option" + (type === avatarType ? " selected" : "");
    const cv = document.createElement("canvas");
    cv.width = 96;
    cv.height = 72;
    drawAvatarOn(cv.getContext("2d"), 96, 72, type, myRole || "listener", preview);
    const label = document.createElement("small");
    label.textContent = def.label;
    opt.appendChild(cv);
    opt.appendChild(label);
    opt.onclick = () => {
      avatarType = type;
      localStorage.setItem("vm_avatar", type);
      list.querySelectorAll(".avatar-option").forEach((el) => el.classList.remove("selected"));
      opt.classList.add("selected");
    };
    list.appendChild(opt);
  }
}

// ---- WebRTC通話 -----------------------------------------------------------------
async function startCall() {
  showScreen("call");
  // 役割とセッション限定の呼び名を常時表示する
  $("call-my-name").textContent = myNickname;
  $("call-my-role").textContent = roleLabel(myRole);
  $("call-my-role").className = `role-tag ${myRole}`;
  $("call-peer-name").textContent = peerNickname;
  $("call-peer-role").textContent = roleLabel(peerRole);
  $("call-peer-role").className = `role-tag ${peerRole}`;
  $("remote-label").textContent = `${peerNickname}（${roleLabel(peerRole)}）`;
  $("local-label").textContent = `${myNickname}（${roleLabel(myRole)}）`;
  $("call-status").textContent = "接続中…";
  pendingCandidates = [];

  pc = new RTCPeerConnection(RTC_CONFIG);
  localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
  $("local-video").srcObject = localStream;

  pc.ontrack = (e) => {
    $("remote-video").srcObject = e.streams[0];
  };
  pc.onicecandidate = (e) => {
    if (e.candidate) wsSend({ type: "signal", data: { kind: "ice", candidate: e.candidate } });
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "connected") {
      $("call-status").textContent = "対話中";
      startSessionTimer();
    } else if (["failed", "disconnected"].includes(pc.connectionState)) {
      $("call-status").textContent = "接続が不安定です…";
    }
  };

  if (isInitiator) {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    wsSend({ type: "signal", data: { kind: "offer", sdp: pc.localDescription } });
  }
}

async function handleSignal(data) {
  if (!pc) return;
  if (data.kind === "offer") {
    await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    await flushPendingCandidates();
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    wsSend({ type: "signal", data: { kind: "answer", sdp: pc.localDescription } });
  } else if (data.kind === "answer") {
    await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
    await flushPendingCandidates();
  } else if (data.kind === "ice") {
    if (pc.remoteDescription) {
      await pc.addIceCandidate(new RTCIceCandidate(data.candidate)).catch(() => {});
    } else {
      pendingCandidates.push(data.candidate);
    }
  }
}

async function flushPendingCandidates() {
  for (const c of pendingCandidates) {
    await pc.addIceCandidate(new RTCIceCandidate(c)).catch(() => {});
  }
  pendingCandidates = [];
}

function startSessionTimer() {
  sessionSeconds = 600;
  updateTimerDisplay();
  sessionTimer = setInterval(() => {
    sessionSeconds--;
    updateTimerDisplay();
    if (sessionSeconds <= 0) {
      endCallToSurvey(true);
    }
  }, 1000);
}

function updateTimerDisplay() {
  const m = Math.floor(Math.max(0, sessionSeconds) / 60);
  const s = Math.max(0, sessionSeconds) % 60;
  $("timer-display").textContent = `${m}:${String(s).padStart(2, "0")}`;
}

$("btn-end-call").onclick = () => endCallToSurvey(true);

function endCallToSurvey(sendLeave) {
  if (sendLeave) wsSend({ type: "leave" });
  cleanupCall(false);
  // アンケート初期化
  $("star3").checked = true;
  $("survey-again").checked = false;
  $("survey-comment").value = "";
  $("survey-error").textContent = "";
  showScreen("survey");
}

function cleanupCall(clearRoom = true) {
  if (sessionTimer) { clearInterval(sessionTimer); sessionTimer = null; }
  if (avatarLoop) { cancelAnimationFrame(avatarLoop); avatarLoop = null; }
  if (pc) { pc.close(); pc = null; }
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (rawStream) {
    rawStream.getTracks().forEach((t) => t.stop());
    rawStream = null;
  }
  if (audioCtxRef) {
    audioCtxRef.close().catch(() => {});
    audioCtxRef = null;
    audioAnalyser = null;
  }
  $("remote-video").srcObject = null;
  $("local-video").srcObject = null;
  if (clearRoom) currentRoomId = "";
  pendingCandidates = [];
}

// ---- アンケート -----------------------------------------------------------------
$("survey-form").onsubmit = async (e) => {
  e.preventDefault();
  $("survey-error").textContent = "";
  const rating = parseInt(document.querySelector('input[name="rating"]:checked').value, 10);
  try {
    await api("/api/surveys", "POST", {
      room_id: currentRoomId,
      rating,
      talk_again: $("survey-again").checked,
      comment: $("survey-comment").value.trim(),
    });
    currentRoomId = "";
    showScreen("done");
  } catch (err) {
    $("survey-error").textContent = err.message;
  }
};

$("btn-back-lobby").onclick = () => {
  $("lobby-error").textContent = "";
  showLobby();
};

// ---- ダッシュボード ---------------------------------------------------------
let calYear = new Date().getFullYear();
let calMonth = new Date().getMonth(); // 0始まり
let monthEvents = [];

async function loadDashboard() {
  try {
    const [announcements, posts, stats, history] = await Promise.all([
      api("/api/announcements"),
      api("/api/posts"),
      api("/api/stats"),
      api("/api/surveys/mine"),
    ]);
    renderAnnouncements(announcements);
    renderPosts(posts);
    renderStats(stats);
    renderHistory(history);
    await loadEvents();
  } catch (err) {
    $("lobby-error").textContent = err.message;
  }
}

/* サーバーのUTC日時文字列をDateに変換(オフセット表記が無ければUTCとみなす) */
function parseUTC(s) {
  return new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z");
}

function renderStats(stats) {
  $("stat-online").textContent = stats.online;
  $("stat-speakers").textContent = stats.waiting_speakers;
  $("stat-listeners").textContent = stats.waiting_listeners;
  $("stat-users").textContent = stats.total_users;
}

function renderAnnouncements(items) {
  $("announcement-list").innerHTML = items.length
    ? items
        .map(
          (a) => `<li>
            <div class="a-title">${escapeHtml(a.title)}</div>
            <div class="a-body">${escapeHtml(a.body)}</div>
            <div class="a-date">${parseUTC(a.created_at).toLocaleDateString("ja-JP")}</div>
          </li>`
        )
        .join("")
    : '<li class="empty-note">お知らせはありません</li>';
}

function renderPosts(items) {
  $("post-list").innerHTML = items.length
    ? items
        .map(
          (p) => `<li>
            <div class="p-meta"><strong>${escapeHtml(p.username)}</strong>
              ${parseUTC(p.created_at).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</div>
            <div class="p-body">${escapeHtml(p.body)}</div>
          </li>`
        )
        .join("")
    : '<li class="empty-note">まだ投稿がありません。最初のメッセージを書いてみましょう!</li>';
}

function renderHistory(items) {
  $("history-list").innerHTML = items.length
    ? items
        .map(
          (s) => `<li>
            <span class="h-stars">${"★".repeat(s.rating)}${"☆".repeat(5 - s.rating)}</span>
            <span class="h-comment">${escapeHtml(s.comment) || "(コメントなし)"}</span>
            <span class="h-date">${parseUTC(s.created_at).toLocaleDateString("ja-JP")}</span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">まだ通話履歴がありません</li>';
}

// ---- イベントカレンダー --------------------------------------------------------
async function loadEvents() {
  const month = `${calYear}-${String(calMonth + 1).padStart(2, "0")}`;
  monthEvents = await api(`/api/events?month=${month}`);
  renderCalendar();
  renderEventList();
}

function renderCalendar() {
  $("cal-title").textContent = `${calYear}年${calMonth + 1}月`;
  const eventDays = new Set(monthEvents.map((e) => parseInt(e.date.slice(8), 10)));
  const today = new Date();
  const firstDow = new Date(calYear, calMonth, 1).getDay();
  const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();

  let html = ["日", "月", "火", "水", "木", "金", "土"]
    .map((d) => `<div class="dow">${d}</div>`)
    .join("");
  html += "<div></div>".repeat(firstDow);
  for (let d = 1; d <= daysInMonth; d++) {
    const isToday =
      d === today.getDate() && calMonth === today.getMonth() && calYear === today.getFullYear();
    const cls = ["day", isToday ? "today" : "", eventDays.has(d) ? "has-event" : ""]
      .filter(Boolean)
      .join(" ");
    const dateStr = `${calYear}-${String(calMonth + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    html += `<div class="${cls}" data-date="${dateStr}">${d}</div>`;
  }
  $("calendar").innerHTML = html;

  // 日付クリックでイベント追加フォームに反映
  $("calendar").querySelectorAll(".day").forEach((el) => {
    el.onclick = () => { $("event-date").value = el.dataset.date; };
  });
}

function renderEventList() {
  $("event-list").innerHTML = monthEvents.length
    ? monthEvents
        .map(
          (e) => `<li>
            <span class="e-date">${parseInt(e.date.slice(5, 7), 10)}/${parseInt(e.date.slice(8), 10)}</span>
            <span>${escapeHtml(e.title)}</span>
            <span class="e-user">by ${escapeHtml(e.username)}</span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">今月のイベントはありません</li>';
}

$("cal-prev").onclick = () => {
  calMonth--;
  if (calMonth < 0) { calMonth = 11; calYear--; }
  loadEvents().catch(() => {});
};
$("cal-next").onclick = () => {
  calMonth++;
  if (calMonth > 11) { calMonth = 0; calYear++; }
  loadEvents().catch(() => {});
};

$("event-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/events", "POST", {
      title: $("event-title").value.trim(),
      date: $("event-date").value,
    });
    $("event-title").value = "";
    // 追加したイベントの月を表示
    const [y, m] = $("event-date").value.split("-").map(Number);
    calYear = y;
    calMonth = m - 1;
    await loadEvents();
  } catch (err) {
    $("lobby-error").textContent = err.message;
  }
};

// ---- 掲示板 -------------------------------------------------------------------
$("post-form").onsubmit = async (e) => {
  e.preventDefault();
  const body = $("post-body").value.trim();
  if (!body) return;
  try {
    await api("/api/posts", "POST", { body });
    $("post-body").value = "";
    renderPosts(await api("/api/posts"));
  } catch (err) {
    $("lobby-error").textContent = err.message;
  }
};

// オンライン統計をダッシュボード表示中だけ定期更新
setInterval(async () => {
  if (token && !$("screen-lobby").classList.contains("hidden")) {
    try {
      renderStats(await api("/api/stats"));
    } catch { /* 一時的な失敗は無視 */ }
  }
}, 15000);

// ---- 初期化 -----------------------------------------------------------------
(async function init() {
  if (!token) {
    showScreen("auth");
    return;
  }
  try {
    const me = await api("/api/me");
    setLoggedIn(token, me.username);
  } catch {
    logout();
  }
})();
