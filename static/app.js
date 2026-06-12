/* VideoMatch フロントエンド
 * 画面遷移: auth → lobby → waiting → consent → call → survey → done → lobby
 */

const API_BASE = ""; // 同一オリジンで配信。別ホストに置く場合はここを変更

// ---- 状態 -------------------------------------------------------------------
let token = localStorage.getItem("vm_token") || "";
let username = localStorage.getItem("vm_username") || "";
let userRole = "user";       // "user" | "moderator" | "site_admin" | "system_admin"
let sessionMinutes = 10;     // サイト設定から取得するセッション時間(分)

// サイト設定(/api/config)。ダッシュボード表示時に取得して反映する
let siteConfig = {
  role_matching: true,
  anonymous_mode: true,
  survey_enabled: true,
  survey_question: "相手の話を「聴けた」と感じましたか？",
  modes: { toon: true, real: true, still: true, camera: false },
};

// "/login" はサブサイト(企業向け)のログインページ。サイトIDの入力が必要で自己登録は不可
const IS_SUB_LOGIN = location.pathname === "/login";
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
let lastCallId = "";     // 直近の通話ID(通報・ブロックで使う)
let callTopic = "";      // 話題カード
let sessionRound = 1;    // 役割交代つきセッションの回数(1→2)
let chatLog = [];        // セッション内チャット(終了画面で破棄)
let micMuted = false;
let camOff = false;
let screenStream = null; // 画面共有中のストリーム

const RTC_CONFIG = {
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};

// ---- ユーティリティ -----------------------------------------------------------
const $ = (id) => document.getElementById(id);

const roleLabel = (role) => (role === "speaker" ? "話し手" : role === "listener" ? "聞き手" : "");
const roleParen = (role) => (role && role !== "any" ? `（${roleLabel(role)}）` : "");

/* アバターの色分けに使う役割。役割なしのときは呼び名から安定したランダム色 */
function avatarColorRole() {
  if (myRole && myRole !== "any") return myRole;
  let h = 0;
  for (const ch of myNickname || "") h += ch.charCodeAt(0);
  return h % 2 ? "speaker" : "listener";
}

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

function setLoggedIn(newToken, newUsername, newRole, mustChangePassword) {
  token = newToken;
  username = newUsername;
  userRole = newRole || "user";
  localStorage.setItem("vm_token", token);
  localStorage.setItem("vm_username", username);
  if (mustChangePassword) {
    // 初期パスワードのままなので、変更が済むまで他の画面には進ませない
    $("pw-error").textContent = "";
    showScreen("password");
    return;
  }
  $("user-name").textContent = `👤 ${username}`;
  $("user-info").classList.remove("hidden");
  $("btn-admin").classList.toggle("hidden", !["site_admin", "system_admin"].includes(userRole));
  $("btn-sysadmin").classList.toggle("hidden", userRole !== "system_admin");
  $("dash-username").textContent = username;
  showLobby();
}

/* ロビー(ダッシュボード)を表示し、データを読み込む */
function showLobby() {
  activeRoomConfig = null; // ルームの通話設定上書きを解除
  showScreen("lobby");
  loadDashboard();
  ensureWS();
  checkWarnings(); // 未確認の警告があればポップアップ表示
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
  userRole = "user";
  $("btn-admin").classList.add("hidden");
  $("btn-sysadmin").classList.add("hidden");
  localStorage.removeItem("vm_token");
  localStorage.removeItem("vm_username");
  cleanupCall();
  if (ws) { ws.close(); ws = null; }
  $("user-info").classList.add("hidden");
  showScreen("auth");
}

// ---- 認証画面 -----------------------------------------------------------------
let authMode = "login";

// サブサイトのログインページ: サイトID入力を表示し、自己登録タブを隠す
if (IS_SUB_LOGIN) {
  $("auth-site-label").classList.remove("hidden");
  $("auth-site").required = true;
  $("tab-register").classList.add("hidden");
}

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
    const body = {
      username: $("auth-username").value.trim(),
      password: $("auth-password").value,
    };
    if (IS_SUB_LOGIN) body.site = $("auth-site").value.trim();
    const data = await api(`/api/${authMode === "login" ? "login" : "register"}`, "POST", body);
    setLoggedIn(data.token, data.username, data.role, data.must_change_password);
  } catch (err) {
    $("auth-error").textContent = err.message;
  }
};

$("btn-logout").onclick = logout;

// ---- 初回パスワード変更 ---------------------------------------------------------
$("password-form").onsubmit = async (e) => {
  e.preventDefault();
  $("pw-error").textContent = "";
  try {
    await api("/api/password", "POST", {
      current_password: $("pw-current").value,
      new_password: $("pw-new").value,
    });
    $("pw-current").value = "";
    $("pw-new").value = "";
    const me = await api("/api/me");
    setLoggedIn(token, me.username, me.role, false);
  } catch (err) {
    $("pw-error").textContent = err.message;
  }
};

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
      if (msg.role === "any") {
        $("waiting-role").textContent = "";
        $("waiting-note").textContent = "相手が見つかり次第、自動的にマッチングされます。";
      } else {
        const opposite = msg.role === "speaker" ? "listener" : "speaker";
        $("waiting-role").textContent = `あなたの役割: ${roleLabel(msg.role)}`;
        $("waiting-note").textContent = `${roleLabel(opposite)}の方が見つかり次第、自動的にマッチングされます。`;
      }
      showScreen("waiting");
      break;
    }

    case "matched":
      currentRoomId = msg.room_id;
      lastCallId = msg.room_id;
      playMatchSound();
      myRole = msg.my_role;
      myNickname = msg.my_nickname;
      peerRole = msg.peer_role;
      peerNickname = msg.peer_nickname;
      $("consent-peer").textContent = peerNickname;
      $("consent-peer-role").textContent = roleParen(peerRole);
      $("consent-my-name").textContent = myNickname;
      $("consent-my-role").textContent = roleParen(myRole);
      $("consent-my-label").textContent = siteConfig.anonymous_mode ? "あなたの呼び名" : "あなたの表示名";
      $("consent-note").textContent = siteConfig.anonymous_mode
        ? "呼び名はこのセッション限定でランダムに割り振られます。本名やユーザー名は相手に伝わりません。"
        : "このサイトではユーザー名がそのまま相手に表示されます。";
      $("consent-status").textContent = "";
      $("btn-consent-ok").disabled = false;
      $("btn-consent-ng").disabled = false;
      // ロビー通話で話し手なら、今日の話題を自分で設定できる
      $("topic-input-wrap").classList.toggle(
        "hidden", !(myRole === "speaker" && !activeRoomConfig)
      );
      $("topic-input").value = "";
      ensureAllowedAvatar();
      if (["toon", "real"].includes(AVATARS[avatarType].mode)) loadFaceLandmarker(); // 先読み
      if (AVATARS[avatarType].mode === "real") loadVRM(avatarType);
      renderAvatarPicker();
      showScreen("consent");
      break;

    case "call_start":
      isInitiator = msg.initiator;
      callTopic = msg.topic || "";
      await startCall();
      break;

    case "chat":
      appendChat({ mine: false, sender: msg.sender, text: msg.text });
      if ($("chat-panel").classList.contains("hidden")) {
        $("btn-chat").classList.add("attention"); // 未読あり
      }
      break;

    case "swap_offer":
      // 相手が役割交代を希望している
      if (!$("screen-call").classList.contains("hidden")) {
        $("swap-text").textContent = "相手が役割交代を希望しています。交代してもう1回話しますか？";
        $("swap-overlay").classList.remove("hidden");
      }
      break;

    case "swap_start":
      // 役割を交代して2回目のセッションへ
      sessionRound = 2;
      myRole = msg.my_role;
      peerRole = msg.peer_role;
      $("swap-overlay").classList.add("hidden");
      $("btn-swap-yes").disabled = false;
      $("swap-status").textContent = "";
      updateCallIdentity();
      startSessionTimer();
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
  if (siteConfig.role_matching && !selectedRole) {
    $("lobby-error").textContent = "「話し手」か「聞き手」を選んでください";
    return;
  }
  try {
    activeRoomConfig = null; // 通常マッチングはサイト設定で行う
    if (!ws) await connectWS();
    wsSend({ type: "join_queue", role: siteConfig.role_matching ? selectedRole : "any" });
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
    rawStream = await navigator.mediaDevices.getUserMedia(mediaConstraints());
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
  wsSend({ type: "consent", accept: true, topic: $("topic-input").value.trim() });
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
const faceCur = { x: 0, y: 0, roll: 0, pitch: 0, blinkL: 0, blinkR: 0, jaw: 0, smile: 0, brow: 0 };
const faceTgt = { x: 0, y: 0, roll: 0, pitch: 0, blinkL: 0, blinkR: 0, jaw: 0, smile: 0, brow: 0 };

// うなずきの中立姿勢キャリブレーション(カメラ位置による角度オフセット対策)
let pitchBase = null;
let pitchFrames = 0;

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
        outputFacialTransformationMatrixes: true, // 頭の姿勢(うなずき角度)用
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
  ensureAllowedAvatar();
  const mode = AVATARS[avatarType].mode;

  if (mode === "camera") {
    // 実映像: カメラ映像をそのまま送る(顔解析もアバター描画もしない)
    return rawStream;
  }

  avatarCanvas = document.createElement("canvas");
  avatarCanvas.width = 512;
  avatarCanvas.height = 320; // 表示エリアと同じ16:10(クロップによる頭の見切れを防ぐ)
  avatarCtx = avatarCanvas.getContext("2d");

  if (mode === "still") {
    // 静止画: 呼び名のカードを描いて音声のみで対話する
    const tick = () => {
      drawStillOn(avatarCtx, avatarCanvas.width, avatarCanvas.height, myNickname, avatarColorRole());
      avatarLoop = requestAnimationFrame(tick);
    };
    tick();
    const stream = avatarCanvas.captureStream(5);
    rawStream.getAudioTracks().forEach((t) => stream.addTrack(t));
    return stream;
  }

  // トラッキング用の非表示video。画面にもネットワークにも出さない
  const trackVideo = document.createElement("video");
  trackVideo.muted = true;
  trackVideo.playsInline = true;
  trackVideo.srcObject = new MediaStream(rawStream.getVideoTracks());
  await trackVideo.play().catch(() => {});

  await loadFaceLandmarker();
  if (!faceLandmarker) setupAudioFallback();
  if (mode === "real") {
    await activateVRM(avatarType); // 失敗してもデフォルメ表示にフォールバックして続行
  }

  startAvatarLoop(trackVideo);

  const stream = avatarCanvas.captureStream(24);
  rawStream.getAudioTracks().forEach((t) => stream.addTrack(t));
  return stream;
}

/* 静止画モード: ブランド背景+イニシャルの丸+呼び名のカード */
function drawStillOn(ctx, W, H, name, role) {
  drawAvatarBackground(ctx, W, H);
  const main = role === "speaker" ? "#ee6c4d" : "#3d5a80";
  ctx.fillStyle = main;
  ctx.beginPath();
  ctx.arc(W / 2, H * 0.4, H * 0.22, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.font = `bold ${Math.round(H * 0.18)}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText((name || "？").slice(0, 1), W / 2, H * 0.41);
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = "#293241";
  ctx.font = `${Math.round(H * 0.08)}px sans-serif`;
  ctx.fillText(name || "", W / 2, H * 0.78);
  ctx.fillStyle = "rgba(41, 50, 65, 0.55)";
  ctx.font = `${Math.round(H * 0.055)}px sans-serif`;
  ctx.fillText("音声で対話中", W / 2, H * 0.88);
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
  // うなずき(縦の首振り): 顔姿勢行列から頭の上下の傾きを直接取る。
  // 行列は列優先で、第3列(data[8..10])が顔の前方ベクトル。
  // 下を向くとy成分(data[9])が負になる → pitch正=下向き
  const mtx = res.facialTransformationMatrixes && res.facialTransformationMatrixes[0];
  if (mtx && mtx.data) {
    const d = mtx.data;
    const pitchRad = Math.atan2(-d[9], d[10]);
    // 行列の角度は「カメラから見た角度」のため、カメラが顔より上/下に
    // あると常時オフセットが乗る。通話開始直後の約2秒間の平均を
    // 中立姿勢とみなして差し引く(自動キャリブレーション)
    if (pitchBase === null) {
      pitchBase = pitchRad;
    } else if (pitchFrames < 60) {
      pitchBase += (pitchRad - pitchBase) * 0.1;
    } else if (Math.abs(pitchRad - pitchBase) < 0.12) {
      // 以降は中立付近のゆっくりしたずれ(姿勢の変化など)だけ追従する
      pitchBase += (pitchRad - pitchBase) * 0.01;
    }
    pitchFrames++;
    faceTgt.pitch = Math.max(-1, Math.min(1, (pitchRad - pitchBase) / 0.5)); // 中立から±約30度で振り切り
  }
  const bs = {};
  if (res.faceBlendshapes && res.faceBlendshapes[0]) {
    for (const c of res.faceBlendshapes[0].categories) bs[c.categoryName] = c.score;
  }
  faceTgt.blinkL = bs.eyeBlinkRight || 0; // ミラーなので左右を入れ替える
  faceTgt.blinkR = bs.eyeBlinkLeft || 0;
  faceTgt.jaw = clamp01((bs.jawOpen || 0) * 1.2);
  // 笑顔は検出スコアが低めに出るので増幅して見た目に反映されやすくする
  faceTgt.smile = clamp01(((bs.mouthSmileLeft || 0) + (bs.mouthSmileRight || 0)) / 2 * 1.8);
  faceTgt.brow = (bs.browInnerUp || 0) * 1.3 - ((bs.browDownLeft || 0) + (bs.browDownRight || 0)) / 2;
}

// ---- アバターの種類 -----------------------------------------------------------
// mode: "toon"=デフォルメ(2D Canvas) / "real"=リアル(3D VRM)
const AVATARS = {
  maru: { label: "まる", mode: "toon" },
  neko: { label: "ねこ", mode: "toon" },
  usagi: { label: "うさぎ", mode: "toon" },
  kuma: { label: "くま", mode: "toon" },
  hito: { label: "ひと", mode: "toon" },
  hinata: { label: "ひなた", mode: "real", url: "models/avatar_real.vrm", thumb: "models/thumbs/avatar_real.jpg" },
  tsumugi: { label: "つむぎ", mode: "real", url: "models/avatar_fem.vrm", thumb: "models/thumbs/avatar_fem.jpg" },
  takeru: { label: "たける", mode: "real", url: "models/avatar_masc.vrm", thumb: "models/thumbs/avatar_masc.jpg" },
  ren: { label: "れん", mode: "real", url: "models/avatar_sample_c.vrm", thumb: "models/thumbs/avatar_sample_c.jpg" },
  seed: { label: "シード", mode: "real", url: "models/avatar_seed.vrm", thumb: "models/thumbs/avatar_seed.jpg" },
  still: { label: "静止画", mode: "still" },
  camera: { label: "実映像", mode: "camera" },
};
let avatarType = localStorage.getItem("vm_avatar") || "maru";
if (!AVATARS[avatarType]) avatarType = "maru";

/* 選択中のアバターが許可されていなければ、許可されたものに切り替える */
function ensureAllowedAvatar() {
  const modes = effectiveModes() || { toon: true, real: true, still: true, camera: false };
  if (modes[AVATARS[avatarType].mode]) return;
  for (const t of ["maru", "hinata", "still", "camera"]) {
    if (modes[AVATARS[t].mode]) {
      avatarType = t;
      return;
    }
  }
}

function drawAvatar() {
  const W = avatarCanvas.width, H = avatarCanvas.height;
  if (AVATARS[avatarType].mode === "real" && vrmActive && vrmActive.type === avatarType) {
    updateVRMFrame();
    drawAvatarBackground(avatarCtx, W, H);
    avatarCtx.drawImage(vrmCtx.canvas, 0, 0, W, H);
    return;
  }
  // デフォルメ(2D)。リアル選択中でも3Dの読み込みが終わるまでは「まる」でつなぐ
  const type = AVATARS[avatarType].mode === "real" ? "maru" : avatarType;
  drawAvatarOn(avatarCtx, W, H, type, avatarColorRole(), faceCur);
  if (AVATARS[avatarType].mode === "real") {
    avatarCtx.fillStyle = "rgba(41, 50, 65, 0.7)";
    avatarCtx.font = `${Math.round(H / 24)}px sans-serif`;
    avatarCtx.textAlign = "center";
    avatarCtx.fillText("3Dアバターを準備中…", W / 2, H - H / 18);
  }
}

// ---- リアルモード(3D VRMアバター) ----------------------------------------------
let vrmCtx = null;        // 共有のレンダラー類 { THREE, renderer, scene, camera, clock, canvas }
let vrmCtxPromise = null;
const vrmModels = {};     // type -> Promise<{ vrm } | null> モデルキャッシュ
let vrmActive = null;     // { model, type } 現在シーンに載っているモデル

function loadVRMContext() {
  if (!vrmCtxPromise) {
    vrmCtxPromise = (async () => {
      const THREE = await import("three");
      const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
      const { VRMLoaderPlugin, VRMUtils } = await import("@pixiv/three-vrm");

      const canvas = document.createElement("canvas");
      canvas.width = 512;
      canvas.height = 320; // 表示エリアと同じ16:10。クロップで頭が切れないように
      const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
      renderer.setPixelRatio(1);

      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(28, 512 / 320, 0.1, 20);
      const keyLight = new THREE.DirectionalLight(0xffffff, Math.PI * 0.9);
      keyLight.position.set(0.5, 1.2, 1.5);
      scene.add(keyLight);
      scene.add(new THREE.AmbientLight(0xffffff, Math.PI * 0.45));

      vrmCtx = { THREE, GLTFLoader, VRMLoaderPlugin, VRMUtils, renderer, scene, camera, clock: new THREE.Clock(), canvas };
      return vrmCtx;
    })().catch((err) => {
      console.warn("3D描画を初期化できませんでした", err);
      vrmCtxPromise = null;
      return null;
    });
  }
  return vrmCtxPromise;
}

function loadVRM(type) {
  const def = AVATARS[type];
  if (!def || def.mode !== "real") return Promise.resolve(null);
  if (!vrmModels[type]) {
    vrmModels[type] = (async () => {
      const c = await loadVRMContext();
      if (!c) return null;
      const loader = new c.GLTFLoader();
      loader.register((parser) => new c.VRMLoaderPlugin(parser));
      const gltf = await loader.loadAsync(def.url);
      const vrm = gltf.userData.vrm;
      c.VRMUtils.rotateVRM0(vrm); // VRM0系モデルでも正面を向くように

      // VRM0系は正規化ボーンのX軸・Z軸の回転方向がVRM1と逆になる
      const axisSign = vrm.meta && vrm.meta.metaVersion === "0" ? -1 : 1;
      // Tポーズのままだと腕が映り込むので下ろす
      const lArm = vrm.humanoid.getNormalizedBoneNode("leftUpperArm");
      const rArm = vrm.humanoid.getNormalizedBoneNode("rightUpperArm");
      if (lArm) lArm.rotation.z = -1.2 * axisSign;
      if (rArm) rArm.rotation.z = 1.2 * axisSign;
      vrm.humanoid.update();
      return { vrm, axisSign };
    })().catch((err) => {
      console.warn(`3Dアバター(${def.label})を読み込めませんでした`, err);
      delete vrmModels[type]; // 次回再試行できるようにする
      return null;
    });
  }
  return vrmModels[type];
}

/* 指定モデルをシーンに載せ、顔全体が入る構図にカメラを合わせる */
async function activateVRM(type) {
  const model = await loadVRM(type);
  if (!model || !vrmCtx) {
    vrmActive = null;
    return null;
  }
  if (vrmActive && vrmActive.model !== model) vrmCtx.scene.remove(vrmActive.model.vrm.scene);
  if (!vrmActive || vrmActive.model !== model) vrmCtx.scene.add(model.vrm.scene);
  vrmActive = { model, type };

  vrmCtx.scene.updateMatrixWorld(true);
  const head = model.vrm.humanoid.getNormalizedBoneNode("head");
  const p = new vrmCtx.THREE.Vector3();
  head.getWorldPosition(p);
  // 十分に引いて頭全体+肩を収め、注視点を頭より上にして
  // 頭上に少し空間が空く(アバターがやや下に映る)構図にする
  vrmCtx.camera.position.set(p.x, p.y + 0.1, p.z + 0.95);
  vrmCtx.camera.lookAt(p.x, p.y + 0.07, p.z);
  return vrmActive;
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));

function updateVRMFrame() {
  const { renderer, scene, camera, clock } = vrmCtx;
  const vrm = vrmActive.model.vrm;
  const em = vrm.expressionManager;
  if (em) {
    // VRMのblinkLeftはアバター自身の左目=画面右側。2D側とは左右が逆になる
    em.setValue("blinkLeft", clamp01(faceCur.blinkR));
    em.setValue("blinkRight", clamp01(faceCur.blinkL));
    em.setValue("aa", clamp01(faceCur.jaw * 1.4));
    em.setValue("happy", clamp01(faceCur.smile));
    em.setValue("surprised", clamp01(faceCur.brow * 0.6));
  }
  const head = vrm.humanoid.getNormalizedBoneNode("head");
  if (head) {
    const sign = vrmActive.model.axisSign || 1; // VRM0はX軸・Z軸の回転が逆
    head.rotation.set(
      faceCur.pitch * 0.5 * sign,  // うなずき(下を向くと+)
      faceCur.x / 160 * 0.6,       // 左右の向き
      faceCur.roll * 0.6 * sign    // 首かしげ
    );
  }
  vrm.update(clock.getDelta());
  renderer.render(scene, camera);
}

function drawAvatarBackground(ctx, W, H) {
  const bg = ctx.createLinearGradient(0, 0, 0, H);
  bg.addColorStop(0, "#f8f0e5");
  bg.addColorStop(1, "#e8d5bf");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
}

/* ほっぺ(笑顔で少し上がって大きくなる) */
function drawCheeks(ctx, color, y = 22, f = null) {
  const smile = f ? f.smile : 0;
  ctx.fillStyle = color;
  for (const sx of [-1, 1]) {
    ctx.beginPath();
    ctx.ellipse(sx * 56, y - smile * 5, 13 + smile * 4, 9 + smile * 2, 0, 0, Math.PI * 2);
    ctx.fill();
  }
}

/* 目・まゆ・口(全アバター共通。pで配色と位置を切り替える) */
function drawFaceParts(ctx, f, p) {
  for (const [sx, blink] of [[-1, f.blinkL], [1, f.blinkR]]) {
    const ex = sx * 36, ey = p.eyeY;
    if (f.smile > 0.6 && blink < 0.6) {
      // にっこり目(上向きの弧)
      ctx.strokeStyle = p.closedColor;
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.arc(ex, ey + 5, 11, Math.PI * 1.15, Math.PI * 1.85);
      ctx.stroke();
    } else if (blink > 0.6) {
      // 閉じ目は弧で描く
      ctx.strokeStyle = p.closedColor;
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.arc(ex, ey, 12, 0.15 * Math.PI, 0.85 * Math.PI);
      ctx.stroke();
    } else if (p.eyeStyle === "sclera") {
      // 白目+瞳(笑顔で少し細くなる)
      const squint = (1 - blink * 0.7) * (1 - f.smile * 0.25);
      ctx.fillStyle = p.eyeColor;
      ctx.beginPath();
      ctx.ellipse(ex, ey, 13, 13 * squint, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = p.pupilColor;
      ctx.beginPath();
      ctx.arc(ex, ey + 1, 6, 0, Math.PI * 2);
      ctx.fill();
    } else {
      // 黒目だけ(動物向け)
      const squint = (1 - blink * 0.7) * (1 - f.smile * 0.25);
      ctx.fillStyle = p.pupilColor;
      ctx.beginPath();
      ctx.ellipse(ex, ey, 9, 10 * squint, 0, 0, Math.PI * 2);
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
  // 口(開閉と笑顔に追従。笑顔は口角を大きく上げて分かりやすく)
  const mw = (p.mouthW || 34) + f.smile * 22;
  const open = 3 + f.jaw * (p.openScale || 32);
  const my = p.mouthY;
  ctx.fillStyle = p.mouthColor;
  ctx.beginPath();
  ctx.moveTo(-mw / 2, my - f.smile * 10);
  ctx.quadraticCurveTo(0, my - f.smile * 22 + 4, mw / 2, my - f.smile * 10);
  ctx.quadraticCurveTo(0, my + open, -mw / 2, my - f.smile * 10);
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

  // 等倍スケールで中央寄せ(縦横比が変わっても歪まない)
  const sc = Math.min(W / 480, H / 360);
  ctx.save();
  ctx.translate((W - 480 * sc) / 2, (H - 360 * sc) / 2);
  ctx.scale(sc, sc); // 以降は480x360の座標系で描く

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
  // うなずき(縦の首振り)は顔パーツ全体を上下にずらして表現する
  const fShift = (f.pitch || 0) * 14;

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
    ctx.save();
    ctx.translate(0, fShift);
    // 顔のパネル(うっすら明るく)
    ctx.fillStyle = "rgba(255, 255, 255, 0.12)";
    ctx.beginPath();
    ctx.ellipse(0, 16, 66, 58, 0, 0, Math.PI * 2);
    ctx.fill();
    drawCheeks(ctx, "rgba(255, 255, 255, 0.25)", 22, f);
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#27313f",
      browColor: "rgba(255, 255, 255, 0.85)", closedColor: "#fff",
      mouthColor: "#27313f", eyeY: -12, mouthY: 38,
    });
    ctx.restore();
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
    ctx.save();
    ctx.translate(0, fShift);
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
    drawCheeks(ctx, "rgba(238, 108, 77, 0.22)", 22, f);
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#3a4250",
      browColor: "rgba(70, 80, 95, 0.7)", closedColor: "#3a4250",
      mouthColor: "#7a4a52", eyeY: -16, mouthY: 42, mouthW: 26, openScale: 24,
    });
    ctx.restore();
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
    ctx.save();
    ctx.translate(0, fShift);
    // 鼻
    ctx.fillStyle = "#e88a96";
    ctx.beginPath();
    ctx.moveTo(-6, 20);
    ctx.lineTo(6, 20);
    ctx.lineTo(0, 29);
    ctx.closePath();
    ctx.fill();
    drawCheeks(ctx, "rgba(238, 108, 77, 0.25)", 22, f);
    drawFaceParts(ctx, f, {
      eyeStyle: "dot", pupilColor: "#4a3f38",
      browColor: "rgba(120, 100, 85, 0.7)", closedColor: "#4a3f38",
      mouthColor: "#7a4a52", eyeY: -14, mouthY: 40, mouthW: 24, openScale: 22,
    });
    ctx.restore();
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
    ctx.save();
    ctx.translate(0, fShift);
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
    drawCheeks(ctx, "rgba(238, 108, 77, 0.2)", 22, f);
    drawFaceParts(ctx, f, {
      eyeStyle: "dot", pupilColor: "#3f3128",
      browColor: "rgba(90, 70, 52, 0.8)", closedColor: "#3f3128",
      mouthColor: "#6b4b38", eyeY: -16, mouthY: 42, mouthW: 26, openScale: 24,
    });
    ctx.restore();
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
    ctx.save();
    ctx.translate(0, fShift);
    drawCheeks(ctx, "rgba(238, 108, 77, 0.18)", 26, f);
    drawFaceParts(ctx, f, {
      eyeStyle: "sclera", eyeColor: "#fff", pupilColor: "#3a3026",
      browColor: "#4d3a2c", closedColor: "#3a3026",
      mouthColor: "#a8493f", eyeY: -6, mouthY: 44,
    });
    ctx.restore();
  }

  ctx.restore();
  ctx.restore();
}

/* 選択中の表示モードに応じた注意書き */
function updateAvatarModeNote() {
  const mode = AVATARS[avatarType].mode;
  $("avatar-mode-note").textContent =
    mode === "camera"
      ? "⚠ 実映像では、あなたのカメラ映像がそのまま相手に表示されます。"
      : mode === "still"
        ? "静止画と音声で対話します。カメラ映像は使われません。"
        : "カメラ映像は顔の動きの読み取りだけに使い、相手にはアバターのみが表示されます。";
}

/* 同意画面の表示モード選択サムネイルを描画する */
function renderAvatarPicker() {
  const list = $("avatar-list");
  list.innerHTML = "";
  const allowed = effectiveModes() || { toon: true, real: true, still: true, camera: false };
  const preview = { x: 0, y: 0, roll: 0, pitch: 0, blinkL: 0, blinkR: 0, jaw: 0.15, smile: 0.45, brow: 0.1 };
  const groups = [
    ["toon", "デフォルメモード"],
    ["real", "リアルモード"],
    ["other", "その他"],
  ];
  for (const [group, title] of groups) {
    const entries = Object.entries(AVATARS).filter(([, def]) => {
      const g = def.mode === "toon" || def.mode === "real" ? def.mode : "other";
      return g === group && allowed[def.mode];
    });
    if (!entries.length) continue;
    const heading = document.createElement("div");
    heading.className = "avatar-mode-title";
    heading.textContent = title;
    list.appendChild(heading);
    const row = document.createElement("div");
    row.className = "avatar-row";
    for (const [type, def] of entries) {
      const opt = document.createElement("button");
      opt.type = "button";
      opt.className = "avatar-option" + (type === avatarType ? " selected" : "");
      if (def.mode === "real") {
        // VRMメタ情報から抽出したサムネイル画像
        const img = document.createElement("img");
        img.src = def.thumb;
        img.alt = def.label;
        img.width = 96;
        img.height = 72;
        opt.appendChild(img);
      } else {
        const cv = document.createElement("canvas");
        cv.width = 96;
        cv.height = 72;
        const c2 = cv.getContext("2d");
        if (def.mode === "still") {
          drawStillOn(c2, 96, 72, myNickname || "？", avatarColorRole());
        } else if (def.mode === "camera") {
          c2.fillStyle = "#293241";
          c2.fillRect(0, 0, 96, 72);
          c2.font = "28px sans-serif";
          c2.textAlign = "center";
          c2.textBaseline = "middle";
          c2.fillText("🎥", 48, 36);
        } else {
          drawAvatarOn(c2, 96, 72, type, avatarColorRole(), preview);
        }
        opt.appendChild(cv);
      }
      const label = document.createElement("small");
      label.textContent = def.label;
      opt.appendChild(label);
      opt.onclick = () => {
        avatarType = type;
        localStorage.setItem("vm_avatar", type);
        list.querySelectorAll(".avatar-option").forEach((el) => el.classList.remove("selected"));
        opt.classList.add("selected");
        updateAvatarModeNote();
        if (def.mode === "real") loadVRM(type); // 3Dモデルを先読み
      };
      row.appendChild(opt);
    }
    list.appendChild(row);
  }
  updateAvatarModeNote();
}

// ---- WebRTC通話 -----------------------------------------------------------------
/* 役割と呼び名の常時表示を更新する(役割交代時にも使う) */
function updateCallIdentity() {
  const hasRoles = myRole !== "any";
  $("call-my-name").textContent = myNickname;
  $("call-peer-name").textContent = peerNickname;
  $("call-my-role").classList.toggle("hidden", !hasRoles);
  $("call-peer-role").classList.toggle("hidden", !hasRoles);
  if (hasRoles) {
    $("call-my-role").textContent = roleLabel(myRole);
    $("call-my-role").className = `role-tag ${myRole}`;
    $("call-peer-role").textContent = roleLabel(peerRole);
    $("call-peer-role").className = `role-tag ${peerRole}`;
  }
  $("remote-label").textContent = `${peerNickname}${roleParen(peerRole)}`;
  $("local-label").textContent = `${myNickname}${roleParen(myRole)}`;
}

async function startCall() {
  showScreen("call");
  updateCallIdentity();
  // 話題カード
  $("topic-banner").classList.toggle("hidden", !callTopic);
  $("topic-text").textContent = callTopic;
  // 通話機能の初期化(サイト設定で利用可否が変わる)
  sessionRound = 1;
  chatLog = [];
  micMuted = false;
  camOff = false;
  $("chat-messages").innerHTML = "";
  $("chat-panel").classList.add("hidden");
  $("swap-overlay").classList.add("hidden");
  $("btn-swap-yes").disabled = false;
  $("swap-status").textContent = "";
  const feats = siteConfig.features || {};
  $("btn-mute").classList.toggle("hidden", !feats.mute);
  $("btn-cam").classList.toggle("hidden", !feats.camera_toggle);
  $("btn-share").classList.toggle("hidden", !feats.screenshare);
  $("btn-chat").classList.toggle("hidden", !feats.chat);
  $("btn-chat").classList.remove("attention");
  $("btn-mute").classList.remove("active");
  $("btn-cam").classList.remove("active");
  $("btn-share").classList.remove("active");
  $("btn-mute").textContent = "🎤 ミュート";
  $("btn-cam").textContent = "📷 映像オフ";
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
  if (sessionTimer) clearInterval(sessionTimer);
  // ルーム参加中はルームのセッション時間がサイト設定より優先される
  const minutes = (activeRoomConfig && activeRoomConfig.session_minutes) || sessionMinutes;
  sessionSeconds = minutes * 60;
  updateTimerDisplay();
  sessionTimer = setInterval(() => {
    sessionSeconds--;
    updateTimerDisplay();
    if (sessionSeconds <= 0) {
      clearInterval(sessionTimer);
      sessionTimer = null;
      onSessionTimeUp();
    }
  }, 1000);
}

/* セッション時間終了。役割ありの1回目なら役割交代を提案する(10分×2回) */
function onSessionTimeUp() {
  const canSwap =
    siteConfig.role_swap_enabled &&
    sessionRound === 1 &&
    (myRole === "speaker" || myRole === "listener");
  if (canSwap) {
    $("swap-text").textContent = "役割を交代して、もう1回話しますか？";
    $("swap-overlay").classList.remove("hidden");
  } else {
    endCallToSurvey(true);
  }
}

$("btn-swap-yes").onclick = () => {
  wsSend({ type: "swap_request" });
  $("btn-swap-yes").disabled = true;
  $("swap-status").textContent = "相手の同意を待っています…";
};
$("btn-swap-no").onclick = () => {
  $("swap-overlay").classList.add("hidden");
  endCallToSurvey(true);
};

function updateTimerDisplay() {
  const m = Math.floor(Math.max(0, sessionSeconds) / 60);
  const s = Math.max(0, sessionSeconds) % 60;
  $("timer-display").textContent = `${m}:${String(s).padStart(2, "0")}`;
}

$("btn-end-call").onclick = () => endCallToSurvey(true);

// ---- 通話コントロール(ミュート・映像オフ・画面共有・チャット) --------------------
$("btn-mute").onclick = () => {
  micMuted = !micMuted;
  if (localStream) localStream.getAudioTracks().forEach((t) => (t.enabled = !micMuted));
  $("btn-mute").textContent = micMuted ? "🔇 ミュート中" : "🎤 ミュート";
  $("btn-mute").classList.toggle("active", micMuted);
};

$("btn-cam").onclick = () => {
  camOff = !camOff;
  if (localStream) localStream.getVideoTracks().forEach((t) => (t.enabled = !camOff));
  $("btn-cam").textContent = camOff ? "🚫 映像オフ中" : "📷 映像オフ";
  $("btn-cam").classList.toggle("active", camOff);
};

$("btn-share").onclick = async () => {
  if (!pc) return;
  if (screenStream) {
    stopScreenShare();
    return;
  }
  try {
    screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true });
  } catch {
    return; // キャンセル
  }
  const track = screenStream.getVideoTracks()[0];
  const sender = pc.getSenders().find((s) => s.track && s.track.kind === "video");
  if (sender) await sender.replaceTrack(track);
  $("local-video").srcObject = screenStream;
  $("btn-share").textContent = "🖥️ 共有を停止";
  $("btn-share").classList.add("active");
  track.onended = stopScreenShare; // ブラウザの「共有を停止」にも追従
};

function stopScreenShare() {
  if (!screenStream) return;
  screenStream.getTracks().forEach((t) => t.stop());
  screenStream = null;
  const original = localStream && localStream.getVideoTracks()[0];
  const sender = pc && pc.getSenders().find((s) => s.track && s.track.kind === "video");
  if (sender && original) sender.replaceTrack(original);
  $("local-video").srcObject = localStream;
  $("btn-share").textContent = "🖥️ 画面共有";
  $("btn-share").classList.remove("active");
}

$("btn-chat").onclick = () => {
  const panel = $("chat-panel");
  panel.classList.toggle("hidden");
  $("btn-chat").classList.remove("attention");
  if (!panel.classList.contains("hidden")) $("chat-input").focus();
};

$("chat-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text) return;
  wsSend({ type: "chat", text });
  appendChat({ mine: true, sender: myNickname, text });
  $("chat-input").value = "";
};

function appendChat(msg) {
  const time = new Date().toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
  chatLog.push({ ...msg, time });
  const div = document.createElement("div");
  div.className = "chat-msg" + (msg.mine ? " mine" : "");
  div.innerHTML = `<small>${escapeHtml(msg.sender)} ${time}</small><span>${escapeHtml(msg.text)}</span>`;
  $("chat-messages").appendChild(div);
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
}

// ---- アンケート(複数設問対応)とチャットログ -------------------------------------
function surveyQuestions() {
  return siteConfig.survey_questions && siteConfig.survey_questions.length
    ? siteConfig.survey_questions
    : [siteConfig.survey_question];
}

function buildSurveyUI() {
  $("survey-questions-wrap").innerHTML = surveyQuestions()
    .map(
      (q, i) => `
      <label>${escapeHtml(q)}</label>
      <div class="stars">
        ${[5, 4, 3, 2, 1]
          .map(
            (v) =>
              `<input type="radio" name="rating${i}" value="${v}" id="q${i}s${v}" ${v === 3 ? "checked" : ""}><label for="q${i}s${v}">★</label>`
          )
          .join("")}
      </div>`
    )
    .join("");
}

function renderChatLogSection() {
  if (!chatLog.length) {
    $("chatlog-wrap").classList.add("hidden");
    return;
  }
  $("chatlog-wrap").classList.remove("hidden");
  $("chatlog-list").innerHTML = chatLog
    .map(
      (m) => `<li><div class="p-meta"><strong>${escapeHtml(m.sender)}</strong> ${m.time}</div>
        <div class="p-body">${escapeHtml(m.text)}</div></li>`
    )
    .join("");
}

$("btn-save-chatlog").onclick = () => {
  const text = chatLog.map((m) => `[${m.time}] ${m.sender}: ${m.text}`).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  a.download = "chatlog.txt";
  a.click();
  URL.revokeObjectURL(a.href);
};

function discardChatLog() {
  chatLog = [];
  $("chatlog-wrap").classList.add("hidden");
}

function endCallToSurvey(sendLeave) {
  if (sendLeave) wsSend({ type: "leave" });
  $("swap-overlay").classList.add("hidden");
  cleanupCall(false);
  if (!siteConfig.survey_enabled) {
    currentRoomId = "";
    if (chatLog.length) {
      // アンケートなしでもチャットログの保存を促してから破棄する
      openModal(
        "チャットログ",
        '<p class="note">この画面を閉じるとチャットログは削除されます。</p>' +
          '<ul class="post-list">' +
          chatLog
            .map(
              (m) => `<li><div class="p-meta"><strong>${escapeHtml(m.sender)}</strong> ${m.time}</div>
                <div class="p-body">${escapeHtml(m.text)}</div></li>`
            )
            .join("") +
          "</ul>",
        [
          { label: "保存する", primary: true, onClick: () => $("btn-save-chatlog").onclick() },
          { label: "閉じる(削除)", onClick: () => { discardChatLog(); closeModal(); showLobby(); } },
        ]
      );
      return;
    }
    showLobby();
    return;
  }
  // アンケート初期化(設問はサイト設定に従う)
  buildSurveyUI();
  renderChatLogSection();
  $("survey-again").checked = false;
  $("survey-comment").value = "";
  $("survey-error").textContent = "";
  showScreen("survey");
}

function cleanupCall(clearRoom = true) {
  if (sessionTimer) { clearInterval(sessionTimer); sessionTimer = null; }
  if (avatarLoop) { cancelAnimationFrame(avatarLoop); avatarLoop = null; }
  pitchBase = null; // 次の通話で中立姿勢を取り直す
  pitchFrames = 0;
  if (screenStream) {
    screenStream.getTracks().forEach((t) => t.stop());
    screenStream = null;
  }
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
  const answers = surveyQuestions().map((_, i) => {
    const el = document.querySelector(`input[name="rating${i}"]:checked`);
    return el ? parseInt(el.value, 10) : 3;
  });
  try {
    await api("/api/surveys", "POST", {
      room_id: currentRoomId,
      rating: answers[0],
      answers,
      talk_again: $("survey-again").checked,
      comment: $("survey-comment").value.trim(),
    });
    currentRoomId = "";
    discardChatLog(); // 終了画面を離れるのでチャットログを破棄
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
    const [announcements, stats, history, config] = await Promise.all([
      api("/api/announcements"),
      api("/api/stats"),
      api("/api/surveys/mine"),
      api("/api/config"),
    ]);
    renderAnnouncements(announcements);
    renderHistory(history);
    sessionMinutes = config.session_minutes || 10;
    siteConfig = config;
    applyBranding();
    applyMatchingUI();
    renderStats(stats);
    await loadTeams();
    await loadRooms();
    renderPosts(await fetchPosts());
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
  $("stat-users").textContent = stats.total_users;
  if (siteConfig.role_matching) {
    $("stat-box-listeners").classList.remove("hidden");
    $("stat-speakers-label").textContent = "話し手 待機";
    $("stat-speakers").textContent = stats.waiting_speakers;
    $("stat-listeners").textContent = stats.waiting_listeners;
  } else {
    // 役割なしマッチングでは待機人数をまとめて表示
    $("stat-box-listeners").classList.add("hidden");
    $("stat-speakers-label").textContent = "待機中";
    $("stat-speakers").textContent = stats.waiting;
  }
}

/* サイト名・キャッチコピーをサイト設定に合わせて表示する */
function applyBranding() {
  const name = siteConfig.site_name || "対話のおけいこ";
  $("site-title").textContent = name;
  // メインブランド以外ではローマ字サブタイトルを出さない
  $("site-subtitle").classList.toggle("hidden", name !== "対話のおけいこ");
  $("dash-tagline").textContent = siteConfig.tagline || "";
  document.title = `${name} - 匿名対話トレーニング`;
}

/* 役割マッチングの有無に応じてマッチングUIを切り替える */
function applyMatchingUI() {
  const rm = siteConfig.role_matching;
  document.querySelector(".role-select").classList.toggle("hidden", !rm);
  const btn = $("btn-start-matching");
  if (!rm) {
    btn.disabled = false;
    btn.textContent = "セッション相手を探す";
  } else if (selectedRole) {
    btn.disabled = false;
    btn.textContent = `${roleLabel(selectedRole)}として相手を探す`;
  } else {
    btn.disabled = true;
    btn.textContent = "役割を選んでください";
  }
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

// ---- チーム -------------------------------------------------------------------
let myTeams = [];
let postScope = "";   // ""=サイト全体 / チームID文字列
let eventScope = "";
let currentTeamId = null;
let currentTeamLeader = false;

function fetchPosts() {
  return api("/api/posts" + (postScope ? `?team_id=${postScope}` : ""));
}

async function loadTeams() {
  myTeams = await api("/api/teams");
  renderTeamPanel();
  // 掲示板・カレンダーのスコープ切替を所属チームで更新
  for (const [selId, cur] of [["post-scope", postScope], ["event-scope", eventScope]]) {
    const el = $(selId);
    el.innerHTML =
      '<option value="">サイト全体</option>' +
      myTeams.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
    el.value = myTeams.some((t) => String(t.id) === cur) ? cur : "";
  }
  postScope = $("post-scope").value;
  eventScope = $("event-scope").value;
}

$("post-scope").onchange = async () => {
  postScope = $("post-scope").value;
  try { renderPosts(await fetchPosts()); } catch (err) { $("lobby-error").textContent = err.message; }
};
$("event-scope").onchange = async () => {
  eventScope = $("event-scope").value;
  loadEvents().catch(() => {});
};

function renderTeamPanel() {
  $("team-list").innerHTML = myTeams.length
    ? myTeams
        .map(
          (t) => `<li>
            <span>${escapeHtml(t.name)}</span>
            ${t.is_leader ? '<span class="role-tag listener">リーダー</span>' : ""}
            <span class="e-user">${t.members}人
              <button class="btn-text" data-team="${t.id}" data-name="${escapeHtml(t.name)}" data-leader="${t.is_leader}">メンバー</button>
            </span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">所属チームはありません</li>';
  $("team-list").querySelectorAll("[data-team]").forEach((btn) => {
    btn.onclick = () => showTeamMembers(btn.dataset.team, btn.dataset.name, btn.dataset.leader === "true");
  });
}

async function showTeamMembers(teamId, name, isLeader) {
  $("team-error").textContent = "";
  try {
    const [data, stats] = await Promise.all([
      api(`/api/teams/${teamId}/members`),
      api(`/api/teams/${teamId}/stats`),
    ]);
    currentTeamId = teamId;
    currentTeamLeader = isLeader;
    $("team-detail").classList.remove("hidden");
    $("team-detail-name").textContent = name;
    $("team-detail-stats").textContent = `メンバー${stats.members}人 / セッション${stats.sessions}回`;
    $("team-invite-form").classList.toggle("hidden", !isLeader);
    $("team-member-list").innerHTML = data.members
      .map(
        (m) => `<li>
          <span>${escapeHtml(m.username)}</span>
          ${m.is_leader ? '<span class="role-tag listener">リーダー</span>' : ""}
          ${isLeader && !m.is_leader ? `<span class="h-date">
            <button class="btn-text" data-warnmember="${m.user_id}" data-name="${escapeHtml(m.username)}">警告</button>
            <button class="btn-text danger" data-remove="${m.user_id}">削除</button></span>` : ""}
        </li>`
      )
      .join("");
    $("team-member-list").querySelectorAll("[data-remove]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/teams/${teamId}/members/${btn.dataset.remove}`, "DELETE");
          await loadTeams();
          await showTeamMembers(teamId, name, isLeader);
        } catch (err) {
          $("team-error").textContent = err.message;
        }
      };
    });
    $("team-member-list").querySelectorAll("[data-warnmember]").forEach((btn) => {
      btn.onclick = () => issueWarning(parseInt(btn.dataset.warnmember, 10), btn.dataset.name);
    });
    // チームリーダーには、自チームのメンバーが対象の通報を表示する
    if (isLeader) {
      try {
        const reports = await api(`/api/reports?team_id=${teamId}`);
        if (reports.length) {
          $("team-member-list").insertAdjacentHTML(
            "beforeend",
            `<li class="team-reports-head"><strong>このチームへの通報</strong></li>` +
              reports
                .map(
                  (r) => `<li>
                    <span>${escapeHtml(r.reported)}: ${escapeHtml(r.reason)}</span>
                    <span class="h-date">${parseUTC(r.created_at).toLocaleDateString("ja-JP")} ${r.status === "open" ? "未対応" : "対応済み"}</span>
                  </li>`
                )
                .join("")
          );
        }
      } catch { /* リーダー以外には出さない */ }
    }
  } catch (err) {
    $("team-error").textContent = err.message;
  }
}

$("team-invite-form").onsubmit = async (e) => {
  e.preventDefault();
  $("team-error").textContent = "";
  try {
    await api(`/api/teams/${currentTeamId}/members`, "POST", {
      username: $("team-invite-name").value.trim(),
    });
    $("team-invite-name").value = "";
    await loadTeams();
    const t = myTeams.find((x) => String(x.id) === String(currentTeamId));
    if (t) await showTeamMembers(t.id, t.name, t.is_leader);
  } catch (err) {
    $("team-error").textContent = err.message;
  }
};

// ---- ルーム -------------------------------------------------------------------
let myRooms = [];
let activeRoomConfig = null; // 参加中ルームの有効設定(セッション時間・役割・表示モード)
let editingRoomId = null;    // 編集中のルームID(nullなら新規作成)

/* ルーム参加中はルームの表示モード設定がサイト設定より優先される */
function effectiveModes() {
  return (activeRoomConfig && activeRoomConfig.modes) || siteConfig.modes;
}

async function loadRooms() {
  const enabled = siteConfig.rooms_enabled;
  $("rooms-panel").classList.toggle("hidden", !enabled);
  if (!enabled) return;
  myRooms = await api("/api/rooms");
  $("btn-room-new").classList.toggle("hidden", !siteConfig.can_create_rooms);
  // イベントフォームのルーム連携の選択肢を更新
  const evSel = $("event-room");
  const cur = evSel.value;
  evSel.innerHTML =
    '<option value="">ルーム連携なし</option>' +
    myRooms.map((r) => `<option value="${r.id}">🎥${escapeHtml(r.name)}</option>`).join("");
  evSel.value = myRooms.some((r) => String(r.id) === cur) ? cur : "";
  $("room-list").innerHTML = myRooms.length
    ? myRooms
        .map(
          (r) => `<li>
            <span>${escapeHtml(r.name)}${r.has_passphrase ? " 🔒" : ""}${r.team_name ? ` <span class="role-tag listener">${escapeHtml(r.team_name)}</span>` : ""}</span>
            <span class="e-user">${r.participants}${r.capacity ? "/" + r.capacity : ""}人
              <button class="btn-text" data-join="${r.id}">参加</button>
              ${r.can_manage ? `<button class="btn-text" data-redit="${r.id}">編集</button>
              <button class="btn-text danger" data-rdelete="${r.id}" data-name="${escapeHtml(r.name)}">削除</button>` : ""}
            </span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">ルームはまだありません</li>';

  $("room-list").querySelectorAll("[data-join]").forEach((btn) => {
    btn.onclick = () => joinRoom(parseInt(btn.dataset.join, 10));
  });
  $("room-list").querySelectorAll("[data-redit]").forEach((btn) => {
    btn.onclick = () => openRoomForm(myRooms.find((r) => r.id === parseInt(btn.dataset.redit, 10)));
  });
  $("room-list").querySelectorAll("[data-rdelete]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`ルーム「${btn.dataset.name}」を削除します。よろしいですか？`)) return;
      try {
        await api(`/api/rooms/${btn.dataset.rdelete}`, "DELETE");
        await loadRooms();
      } catch (err) {
        $("room-error").textContent = err.message;
      }
    };
  });
}

async function joinRoom(roomId) {
  $("room-error").textContent = "";
  const room = myRooms.find((r) => r.id === roomId);
  if (!room) return;
  if (room.role_matching && !selectedRole) {
    $("room-error").textContent = "上の「話し手」「聞き手」から役割を選んでから参加してください";
    return;
  }
  let passphrase = "";
  if (room.has_passphrase) {
    passphrase = prompt(`ルーム「${room.name}」の合言葉を入力してください`) || "";
    if (!passphrase) return;
  }
  // ルームの有効設定(セッション時間・表示モード)を通話に適用する
  activeRoomConfig = {
    session_minutes: room.session_minutes,
    role_matching: room.role_matching,
    modes: room.modes,
  };
  try {
    if (!ws) await connectWS();
    wsSend({
      type: "join_queue",
      role: room.role_matching ? selectedRole : "any",
      room_id: roomId,
      passphrase,
    });
  } catch (err) {
    $("room-error").textContent = err.message;
  }
}

function openRoomForm(room) {
  editingRoomId = room ? room.id : null;
  $("room-form-wrap").classList.remove("hidden");
  $("room-form-error").textContent = "";
  $("room-form-title").textContent = room ? `ルームを編集: ${room.name}` : "ルームを作成";
  $("room-form-submit").textContent = room ? "更新" : "作成";
  // 見える範囲の選択肢(所属チーム)
  $("room-team").innerHTML =
    '<option value="">サイト全体</option>' +
    myTeams.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}限定</option>`).join("");
  $("room-name").value = room ? room.name : "";
  $("room-team").value = room && room.team_id ? String(room.team_id) : "";
  $("room-pass").value = room && room.raw ? room.raw.passphrase : "";
  $("room-topic").value = room && room.raw ? room.raw.topic : "";
  $("room-capacity").value = room ? room.capacity : 0;
  $("room-expires").value = "";
  $("room-minutes").value = room && room.raw && room.raw.session_minutes ? room.raw.session_minutes : "";
  $("room-role-matching").value =
    room && room.raw && room.raw.role_matching !== null ? String(room.raw.role_matching) : "";
  const override = !!(room && room.raw && room.raw.modes);
  $("room-modes-override").checked = override;
  $("room-modes-list").classList.toggle("hidden", !override);
  for (const m of ["toon", "real", "still", "camera"]) {
    $(`room-mode-${m}`).checked = override ? room.raw.modes.includes(m) : m !== "camera";
  }
  // ルーム管理者(編集時のみ)
  $("room-managers-wrap").classList.toggle("hidden", !room);
  if (room && room.managers) {
    $("room-manager-list").innerHTML = room.managers.length
      ? room.managers
          .map(
            (m) => `<li><span>${escapeHtml(m.username)}</span>
              <span class="h-date"><button class="btn-text danger" data-rmgr="${m.user_id}">解除</button></span></li>`
          )
          .join("")
      : '<li class="empty-note">追加のルーム管理者はいません</li>';
    $("room-manager-list").querySelectorAll("[data-rmgr]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rooms/${room.id}/managers/${btn.dataset.rmgr}`, "DELETE");
          await loadRooms();
          openRoomForm(myRooms.find((r) => r.id === room.id));
        } catch (err) {
          $("room-form-error").textContent = err.message;
        }
      };
    });
  }
}

$("btn-room-new").onclick = () => openRoomForm(null);
$("room-form-cancel").onclick = () => $("room-form-wrap").classList.add("hidden");
$("room-modes-override").onchange = () =>
  $("room-modes-list").classList.toggle("hidden", !$("room-modes-override").checked);

$("btn-room-manager-add").onclick = async () => {
  const name = $("room-manager-name").value.trim();
  if (!name || !editingRoomId) return;
  try {
    await api(`/api/rooms/${editingRoomId}/managers`, "POST", { username: name });
    $("room-manager-name").value = "";
    await loadRooms();
    openRoomForm(myRooms.find((r) => r.id === editingRoomId));
  } catch (err) {
    $("room-form-error").textContent = err.message;
  }
};

$("room-form").onsubmit = async (e) => {
  e.preventDefault();
  $("room-form-error").textContent = "";
  const body = {
    name: $("room-name").value.trim(),
    team_id: $("room-team").value ? parseInt($("room-team").value, 10) : null,
    passphrase: $("room-pass").value,
    topic: $("room-topic").value.trim(),
    capacity: parseInt($("room-capacity").value || "0", 10),
    expires_hours: $("room-expires").value ? parseInt($("room-expires").value, 10) : null,
    session_minutes: $("room-minutes").value ? parseInt($("room-minutes").value, 10) : null,
    role_matching: $("room-role-matching").value === "" ? null : $("room-role-matching").value === "true",
    modes: $("room-modes-override").checked
      ? ["toon", "real", "still", "camera"].filter((m) => $(`room-mode-${m}`).checked)
      : null,
  };
  try {
    if (editingRoomId) {
      await api(`/api/rooms/${editingRoomId}`, "PUT", body);
    } else {
      await api("/api/rooms", "POST", body);
    }
    $("room-form-wrap").classList.add("hidden");
    await loadRooms();
  } catch (err) {
    $("room-form-error").textContent = err.message;
  }
};

// ---- イベントカレンダー --------------------------------------------------------
async function loadEvents() {
  const month = `${calYear}-${String(calMonth + 1).padStart(2, "0")}`;
  monthEvents = await api(`/api/events?month=${month}` + (eventScope ? `&team_id=${eventScope}` : ""));
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
            <span>${escapeHtml(e.title)}${e.room_id && e.room_name ? ` <button class="btn-text" data-evroom="${e.room_id}">🎥${escapeHtml(e.room_name)}</button>` : ""}</span>
            <span class="e-user">by ${escapeHtml(e.username)}</span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">今月のイベントはありません</li>';
  // 予定からワンタップでルームに参加できる
  $("event-list").querySelectorAll("[data-evroom]").forEach((btn) => {
    btn.onclick = () => {
      const rid = parseInt(btn.dataset.evroom, 10);
      if (myRooms.some((r) => r.id === rid)) {
        joinRoom(rid);
      } else {
        $("lobby-error").textContent = "ルームが見つかりません(削除されたか、参加権限がありません)";
      }
    };
  });
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
      team_id: eventScope ? parseInt(eventScope, 10) : null,
      room_id: $("event-room").value ? parseInt($("event-room").value, 10) : null,
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
    await api("/api/posts", "POST", { body, team_id: postScope ? parseInt(postScope, 10) : null });
    $("post-body").value = "";
    renderPosts(await fetchPosts());
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

// ---- 管理画面(管理者のみ) -----------------------------------------------------
$("btn-admin").onclick = () => showAdmin();
$("btn-admin-back").onclick = () => showLobby();

/* 管理画面のページ切替(ユーザー / サイト設定 / レポート・ログ / チーム・お知らせ) */
function showAdminPage(name) {
  document.querySelectorAll("#screen-admin .admin-page").forEach((el) => el.classList.add("hidden"));
  $(`admin-page-${name}`).classList.remove("hidden");
  document.querySelectorAll("#screen-admin .admin-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.page === name);
  });
}
document.querySelectorAll("#screen-admin .admin-tab").forEach((tab) => {
  tab.onclick = () => showAdminPage(tab.dataset.page);
});

async function showAdmin() {
  $("admin-error").textContent = "";
  showScreen("admin");
  showAdminPage("users");
  try {
    await Promise.all([
      loadAdminUsers(),
      loadAdminSettings(),
      loadAdminAnnouncements(),
      loadAdminTeams(),
      loadAdminReport(),
      loadAdminAudit(),
      loadAdminReports(),
    ]);
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
}

let adminUsers = [];
let adminUserSort = { key: "id", dir: 1 }; // dir: 1=昇順, -1=降順

async function loadAdminUsers() {
  adminUsers = await api("/api/admin/users");
  renderAdminUsers();
}

// 列見出しクリックでソート(同じ列をもう一度押すと昇順/降順を切替)
document.querySelectorAll("#admin-user-head .sortable").forEach((th) => {
  th.dataset.label = th.textContent;
  th.onclick = () => {
    const key = th.dataset.key;
    if (adminUserSort.key === key) {
      adminUserSort.dir *= -1;
    } else {
      adminUserSort = { key, dir: 1 };
    }
    renderAdminUsers();
  };
});

function renderAdminUsers() {
  // 見出しにソート方向(▲▼)を表示
  document.querySelectorAll("#admin-user-head .sortable").forEach((th) => {
    const active = th.dataset.key === adminUserSort.key;
    th.classList.toggle("sorted", active);
    th.textContent = active
      ? `${th.dataset.label} ${adminUserSort.dir === 1 ? "▲" : "▼"}`
      : th.dataset.label;
  });
  const { key, dir } = adminUserSort;
  const sorted = [...adminUsers].sort((a, b) => {
    const va = a[key], vb = b[key];
    if (typeof va === "string" && typeof vb === "string") return va.localeCompare(vb, "ja") * dir;
    return (va === vb ? 0 : va > vb ? 1 : -1) * dir;
  });

  $("admin-user-rows").innerHTML = sorted
    .map((u) => {
      const isSelf = u.username === username;
      const isAdminRole = ["site_admin", "system_admin"].includes(u.role);
      // ロール変更: システム管理者と自分自身は変更不可
      const roleCell = u.role === "system_admin" || isSelf
        ? `<span class="role-tag speaker">${roleLabels[u.role] || u.role}</span>`
        : `<select class="role-select-mini" data-roleuser="${u.id}">
            <option value="user" ${u.role === "user" ? "selected" : ""}>一般</option>
            <option value="moderator" ${u.role === "moderator" ? "selected" : ""}>モデレータ</option>
            <option value="site_admin" ${u.role === "site_admin" ? "selected" : ""}>サイト管理者</option>
          </select>`;
      return `<tr class="${u.is_active ? "" : "row-inactive"}">
        <td>${u.id}</td>
        <td class="nowrap">${escapeHtml(u.username)}</td>
        <td class="nowrap">${roleCell}</td>
        <td class="nowrap">${u.is_active ? "有効" : '<span class="role-tag speaker">無効</span>'}</td>
        <td class="nowrap">${parseUTC(u.created_at).toLocaleDateString("ja-JP")}</td>
        <td>${u.session_count}</td>
        <td class="admin-actions">
          <button class="btn-text" data-history="${u.id}" data-name="${escapeHtml(u.username)}">履歴</button>
          ${isAdminRole ? "" : `<button class="btn-text" data-active="${u.id}" data-next="${!u.is_active}" data-name="${escapeHtml(u.username)}">${u.is_active ? "無効化" : "有効化"}</button>
          <button class="btn-text" data-resetpw="${u.id}" data-name="${escapeHtml(u.username)}">PWリセット</button>
          <button class="btn-text" data-warnuser="${u.id}" data-name="${escapeHtml(u.username)}">警告</button>
          <button class="btn-text danger" data-delete="${u.id}" data-name="${escapeHtml(u.username)}">削除</button>`}
        </td>
      </tr>`;
    })
    .join("");

  $("admin-user-rows").querySelectorAll("[data-history]").forEach((btn) => {
    btn.onclick = () => loadAdminHistory(btn.dataset.history, btn.dataset.name);
  });
  $("admin-user-rows").querySelectorAll("[data-roleuser]").forEach((sel) => {
    sel.onchange = async () => {
      try {
        await api(`/api/admin/users/${sel.dataset.roleuser}/role`, "PUT", { role: sel.value });
        await loadAdminUsers();
      } catch (err) {
        $("admin-error").textContent = err.message;
        await loadAdminUsers(); // 失敗時は表示を元に戻す
      }
    };
  });
  $("admin-user-rows").querySelectorAll("[data-active]").forEach((btn) => {
    btn.onclick = async () => {
      const enable = btn.dataset.next === "true";
      if (!enable && !confirm(`「${btn.dataset.name}」を無効化します。ログインできなくなります。よろしいですか？`)) return;
      try {
        await api(`/api/admin/users/${btn.dataset.active}/active`, "PUT", { is_active: enable });
        await loadAdminUsers();
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
  $("admin-user-rows").querySelectorAll("[data-resetpw]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`「${btn.dataset.name}」のパスワードをリセットします。よろしいですか？`)) return;
      try {
        const r = await api(`/api/admin/users/${btn.dataset.resetpw}/reset_password`, "POST");
        alert(
          `新しい初期パスワード: ${r.password}\n` +
          (r.mailed ? "登録メールアドレスに送信しました。" : "本人に伝えてください(この画面にしか表示されません)。") +
          "\n初回ログイン時にパスワード変更が必要です。"
        );
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
  $("admin-user-rows").querySelectorAll("[data-warnuser]").forEach((btn) => {
    btn.onclick = () => issueWarning(parseInt(btn.dataset.warnuser, 10), btn.dataset.name);
  });
  $("admin-user-rows").querySelectorAll("[data-delete]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`ユーザー「${btn.dataset.name}」と関連データを削除します。よろしいですか？`)) return;
      try {
        await api(`/api/admin/users/${btn.dataset.delete}`, "DELETE");
        await loadAdminUsers();
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
}

// ユーザー一覧のCSVエクスポート
$("btn-export-users").onclick = async () => {
  try {
    const res = await fetch("/api/admin/users/export", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error("エクスポートに失敗しました");
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "users.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

async function loadAdminHistory(userId, name) {
  $("admin-error").textContent = "";
  try {
    const data = await api(`/api/admin/users/${userId}/surveys`);
    $("admin-history-user").textContent = `: ${name}`;
    $("admin-history-list").innerHTML = data.surveys.length
      ? data.surveys
          .map(
            (s) => `<li>
              <span class="h-stars">${"★".repeat(s.rating)}${"☆".repeat(5 - s.rating)}</span>
              <span class="h-comment">${escapeHtml(s.comment) || "(コメントなし)"}</span>
              <span class="h-date">${parseUTC(s.created_at).toLocaleDateString("ja-JP")}</span>
            </li>`
          )
          .join("")
      : '<li class="empty-note">このユーザーのセッション履歴はありません</li>';
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
}

$("admin-user-form").onsubmit = async (e) => {
  e.preventDefault();
  $("admin-error").textContent = "";
  try {
    const r = await api("/api/admin/users", "POST", {
      username: $("new-user-name").value.trim(),
      password: $("new-user-pass").value,
      email: $("new-user-email").value.trim() || null,
    });
    if ($("new-user-email").value.trim() && !r.mailed) {
      $("admin-error").textContent = "ユーザーは作成しましたが、メールは送信できませんでした(SMTP未設定)";
    }
    $("new-user-name").value = "";
    $("new-user-pass").value = "";
    $("new-user-email").value = "";
    await loadAdminUsers();
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

$("btn-export-csv").onclick = async () => {
  try {
    const res = await fetch("/api/admin/report/export", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error("エクスポートに失敗しました");
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "sessions.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

async function loadAdminSettings() {
  const s = await api("/api/admin/settings");
  $("set-site-name").value = s.site_name;
  $("set-tagline").value = s.tagline;
  $("set-minutes").value = s.session_minutes;
  $("set-registration").checked = s.allow_registration;
  $("set-role-matching").checked = s.role_matching;
  $("set-anonymous").checked = s.anonymous_mode;
  $("set-survey").checked = s.survey_enabled;
  $("set-survey-question").value = s.survey_question;
  $("set-mode-toon").checked = s.mode_toon;
  $("set-mode-real").checked = s.mode_real;
  $("set-mode-still").checked = s.mode_still;
  $("set-mode-camera").checked = s.mode_camera;
  $("set-rooms").checked = s.rooms_enabled;
  $("set-feat-mute").checked = s.feature_mute;
  $("set-feat-cam").checked = s.feature_camera_toggle;
  $("set-feat-share").checked = s.feature_screenshare;
  $("set-feat-chat").checked = s.feature_chat;
  $("set-role-swap").checked = s.role_swap_enabled;
  $("set-rematch").checked = s.rematch_priority;
  $("set-topic-mode").value = s.lobby_topic_mode;
  $("set-topic-text").value = s.lobby_topic_text;
  $("set-topic-pool").value = s.topic_pool;
  $("set-survey-questions").value = s.survey_questions;
}

$("admin-settings-form").onsubmit = async (e) => {
  e.preventDefault();
  $("admin-settings-msg").textContent = "";
  if (!["set-mode-toon", "set-mode-real", "set-mode-still", "set-mode-camera"].some((id) => $(id).checked)) {
    $("admin-settings-msg").textContent = "表示モードは少なくとも1つ有効にしてください";
    return;
  }
  try {
    await api("/api/admin/settings", "PUT", {
      site_name: $("set-site-name").value.trim(),
      tagline: $("set-tagline").value.trim(),
      session_minutes: parseInt($("set-minutes").value, 10),
      allow_registration: $("set-registration").checked,
      role_matching: $("set-role-matching").checked,
      anonymous_mode: $("set-anonymous").checked,
      survey_enabled: $("set-survey").checked,
      survey_question: $("set-survey-question").value.trim(),
      mode_toon: $("set-mode-toon").checked,
      mode_real: $("set-mode-real").checked,
      mode_still: $("set-mode-still").checked,
      mode_camera: $("set-mode-camera").checked,
      rooms_enabled: $("set-rooms").checked,
      feature_mute: $("set-feat-mute").checked,
      feature_camera_toggle: $("set-feat-cam").checked,
      feature_screenshare: $("set-feat-share").checked,
      feature_chat: $("set-feat-chat").checked,
      role_swap_enabled: $("set-role-swap").checked,
      rematch_priority: $("set-rematch").checked,
      lobby_topic_mode: $("set-topic-mode").value,
      lobby_topic_text: $("set-topic-text").value.trim(),
      topic_pool: $("set-topic-pool").value.trim(),
      survey_questions: $("set-survey-questions").value.trim(),
    });
    sessionMinutes = parseInt($("set-minutes").value, 10);
    $("admin-settings-msg").textContent = "保存しました";
  } catch (err) {
    $("admin-settings-msg").textContent = err.message;
  }
};

// --- 通報キュー(サイト管理者) ---
async function loadAdminReports() {
  const rows = await api("/api/reports");
  $("report-rows").innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr>
            <td>${parseUTC(r.created_at).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
            <td>${escapeHtml(r.reporter)}</td>
            <td>${escapeHtml(r.reported)}</td>
            <td>${escapeHtml(r.reason)}</td>
            <td>${r.status === "open" ? '<span class="role-tag speaker">未対応</span>' : "対応済み"}</td>
            <td class="admin-actions">
              ${r.status === "open" ? `<button class="btn-text" data-resolve="${r.id}">対応済みにする</button>` : ""}
              <button class="btn-text danger" data-warn-from-report="${r.reported_id}" data-name="${escapeHtml(r.reported)}">警告</button>
            </td>
          </tr>`
        )
        .join("")
    : '<tr><td colspan="6" class="empty-note">通報はありません</td></tr>';
  $("report-rows").querySelectorAll("[data-resolve]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/reports/${btn.dataset.resolve}`, "PUT", { status: "resolved" });
        await loadAdminReports();
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
  $("report-rows").querySelectorAll("[data-warn-from-report]").forEach((btn) => {
    btn.onclick = () => issueWarning(parseInt(btn.dataset.warnFromReport, 10), btn.dataset.name, loadAdminReports);
  });
}

/* 警告文を発令する(サイト管理者・チームリーダー共通) */
async function issueWarning(userId, name, after) {
  const message = prompt(`「${name}」への警告文を入力してください。\n次回ログイン時にポップアップで表示されます。`);
  if (!message || !message.trim()) return;
  try {
    await api("/api/warnings", "POST", { user_id: userId, message: message.trim() });
    alert("警告を発令しました。対象ユーザーのログイン時に表示されます。");
    if (after) await after();
  } catch (err) {
    alert(err.message);
  }
}

// --- CSV一括登録 ---
$("btn-bulk-users").onclick = async () => {
  const csv = $("bulk-csv").value.trim();
  if (!csv) return;
  $("admin-error").textContent = "";
  try {
    const data = await api("/api/admin/users/bulk", "POST", { csv });
    $("bulk-csv").value = "";
    $("bulk-result").innerHTML =
      `<strong>${data.created}件を登録しました。</strong><br>` +
      data.results
        .map(
          (r) =>
            `${escapeHtml(r.username)}: ${r.status === "ok"
              ? "登録" + (r.password ? `（自動生成パスワード: <strong>${escapeHtml(r.password)}</strong>）` : "")
              : escapeHtml(r.status)}`
        )
        .join("<br>") +
      '<br><small>自動生成パスワードは今だけ表示されます。各ユーザーは初回ログイン時に変更が必要です。</small>';
    await loadAdminUsers();
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

// --- 利用レポート ---
async function loadAdminReport() {
  const r = await api("/api/admin/report");
  $("report-numbers").innerHTML = `
    <div class="stat"><span>${r.total_users}</span><small>登録者</small></div>
    <div class="stat"><span>${r.total_sessions}</span><small>総セッション</small></div>
    <div class="stat"><span>${r.sessions_7d}</span><small>直近7日</small></div>
    <div class="stat"><span>${r.avg_rating ?? "-"}</span><small>平均評価</small></div>`;
  const max = Math.max(...r.daily.map((d) => d.count), 1);
  $("report-chart").innerHTML = r.daily
    .map(
      (d) => `<div class="bar-wrap" title="${d.date}: ${d.count}件">
        <div class="bar" style="height:${Math.max((d.count / max) * 100, d.count ? 6 : 0)}%"></div>
        <small>${parseInt(d.date.slice(8), 10)}</small>
      </div>`
    )
    .join("");
  $("report-teams").innerHTML = r.teams.length
    ? r.teams
        .map(
          (t) => `<tr><td>${escapeHtml(t.name)}</td><td>${t.members}</td><td>${t.sessions}</td></tr>`
        )
        .join("")
    : '<tr><td colspan="3" class="empty-note">チームはまだありません</td></tr>';
}

// --- 監査ログ ---
const auditLabels = {
  settings_update: "サイト設定変更",
  user_create: "ユーザー作成",
  user_delete: "ユーザー削除",
  users_bulk: "CSV一括登録",
  role_change: "ロール変更",
  announcement_create: "お知らせ投稿",
  announcement_delete: "お知らせ削除",
  team_create: "チーム作成",
  team_delete: "チーム削除",
  team_member_add: "チームメンバー追加",
  team_leader_set: "リーダー設定",
  room_create: "ルーム作成",
  room_update: "ルーム更新",
  room_delete: "ルーム削除",
  site_create: "サイト作成",
  site_delete: "サイト削除",
};

async function loadAdminAudit() {
  const rows = await api("/api/admin/audit");
  $("audit-list").innerHTML = rows.length
    ? rows
        .map(
          (a) => `<li>
            <div class="a-title">${auditLabels[a.action] || escapeHtml(a.action)}
              <span class="audit-user">by ${escapeHtml(a.username)}</span></div>
            ${a.detail ? `<div class="a-body">${escapeHtml(a.detail)}</div>` : ""}
            <div class="a-date">${parseUTC(a.created_at).toLocaleString("ja-JP")}</div>
          </li>`
        )
        .join("")
    : '<li class="empty-note">まだ操作の記録はありません</li>';
}

// --- チーム管理(サイト管理者) ---
let adminTeamId = null;
let adminTeamName = "";

async function loadAdminTeams() {
  const teams = await api("/api/admin/teams");
  $("admin-team-list").innerHTML = teams.length
    ? teams
        .map(
          (t) => `<li>
            <span>${escapeHtml(t.name)}</span>
            <span class="e-user">${t.members}人${t.leaders.length ? ` / リーダー: ${t.leaders.map(escapeHtml).join(", ")}` : ""}
              <button class="btn-text" data-tmembers="${t.id}" data-name="${escapeHtml(t.name)}">管理</button>
              <button class="btn-text danger" data-tdelete="${t.id}" data-name="${escapeHtml(t.name)}">削除</button>
            </span>
          </li>`
        )
        .join("")
    : '<li class="empty-note">チームはまだありません</li>';
  $("admin-team-list").querySelectorAll("[data-tmembers]").forEach((btn) => {
    btn.onclick = () => showAdminTeam(btn.dataset.tmembers, btn.dataset.name);
  });
  $("admin-team-list").querySelectorAll("[data-tdelete]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`チーム「${btn.dataset.name}」を削除します。チーム限定の投稿・予定も削除されます。よろしいですか？`)) return;
      try {
        await api(`/api/admin/teams/${btn.dataset.tdelete}`, "DELETE");
        $("admin-team-detail").classList.add("hidden");
        await loadAdminTeams();
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
}

async function showAdminTeam(teamId, name) {
  try {
    const data = await api(`/api/teams/${teamId}/members`);
    adminTeamId = teamId;
    adminTeamName = name;
    $("admin-team-detail").classList.remove("hidden");
    $("admin-team-title").textContent = name;
    $("admin-team-members").innerHTML = data.members.length
      ? data.members
          .map(
            (m) => `<li>
              <span>${escapeHtml(m.username)}</span>
              ${m.is_leader ? '<span class="role-tag listener">リーダー</span>' : ""}
              <span class="h-date">
                <button class="btn-text" data-lead="${m.user_id}" data-flag="${!m.is_leader}">${m.is_leader ? "リーダー解除" : "リーダーにする"}</button>
                <button class="btn-text danger" data-tremove="${m.user_id}">外す</button>
              </span>
            </li>`
          )
          .join("")
      : '<li class="empty-note">メンバーがいません</li>';
    $("admin-team-members").querySelectorAll("[data-lead]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/admin/teams/${teamId}/members/${btn.dataset.lead}`, "PUT", {
            is_leader: btn.dataset.flag === "true",
          });
          await loadAdminTeams();
          await showAdminTeam(teamId, name);
        } catch (err) {
          $("admin-error").textContent = err.message;
        }
      };
    });
    $("admin-team-members").querySelectorAll("[data-tremove]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/teams/${teamId}/members/${btn.dataset.tremove}`, "DELETE");
          await loadAdminTeams();
          await showAdminTeam(teamId, name);
        } catch (err) {
          $("admin-error").textContent = err.message;
        }
      };
    });
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
}

$("admin-team-form").onsubmit = async (e) => {
  e.preventDefault();
  $("admin-error").textContent = "";
  try {
    await api("/api/admin/teams", "POST", { name: $("new-team-name").value.trim() });
    $("new-team-name").value = "";
    await loadAdminTeams();
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

$("admin-team-member-form").onsubmit = async (e) => {
  e.preventDefault();
  $("admin-error").textContent = "";
  try {
    await api(`/api/teams/${adminTeamId}/members`, "POST", {
      username: $("team-member-name").value.trim(),
    });
    $("team-member-name").value = "";
    await loadAdminTeams();
    await showAdminTeam(adminTeamId, adminTeamName);
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

async function loadAdminAnnouncements() {
  const items = await api("/api/announcements");
  $("admin-announce-list").innerHTML = items.length
    ? items
        .map(
          (a) => `<li>
            <div class="a-title">${escapeHtml(a.title)}
              <button class="btn-text danger" data-ann="${a.id}">削除</button></div>
            <div class="a-body">${escapeHtml(a.body)}</div>
            <div class="a-date">${parseUTC(a.created_at).toLocaleDateString("ja-JP")}</div>
          </li>`
        )
        .join("")
    : '<li class="empty-note">お知らせはありません</li>';
  $("admin-announce-list").querySelectorAll("[data-ann]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api(`/api/admin/announcements/${btn.dataset.ann}`, "DELETE");
        await loadAdminAnnouncements();
      } catch (err) {
        $("admin-error").textContent = err.message;
      }
    };
  });
}

$("admin-announce-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/announcements", "POST", {
      title: $("ann-title").value.trim(),
      body: $("ann-body").value.trim(),
    });
    $("ann-title").value = "";
    $("ann-body").value = "";
    await loadAdminAnnouncements();
  } catch (err) {
    $("admin-error").textContent = err.message;
  }
};

// ---- 汎用モーダル ---------------------------------------------------------------
function openModal(title, bodyHtml, actions) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHtml;
  $("modal-actions").innerHTML = "";
  for (const act of actions) {
    const btn = document.createElement("button");
    btn.className = act.primary ? "btn-primary" : "btn-secondary";
    btn.textContent = act.label;
    btn.onclick = act.onClick;
    $("modal-actions").appendChild(btn);
  }
  $("modal").classList.remove("hidden");
}

function closeModal() {
  $("modal").classList.add("hidden");
}

// ---- 警告ポップアップ(ログイン時) -------------------------------------------------
async function checkWarnings() {
  try {
    const warnings = await api("/api/warnings/pending");
    if (warnings.length) showWarningModal(warnings, 0);
  } catch { /* 表示できなくても操作は止めない */ }
}

function showWarningModal(warnings, idx) {
  const w = warnings[idx];
  openModal(
    "⚠️ 運営からの警告",
    `<p class="warning-message">${escapeHtml(w.message)}</p>
     <p class="note">${parseUTC(w.created_at).toLocaleString("ja-JP")}</p>`,
    [
      {
        label: "確認しました",
        primary: true,
        onClick: async () => {
          try { await api(`/api/warnings/${w.id}/ack`, "POST"); } catch {}
          if (idx + 1 < warnings.length) {
            showWarningModal(warnings, idx + 1);
          } else {
            closeModal();
          }
        },
      },
    ]
  );
}

// ---- 通報・ブロック -------------------------------------------------------------
async function reportPeer() {
  if (!lastCallId) return;
  const reason = prompt("通報の理由を入力してください（運営とチームリーダーが確認します）");
  if (!reason || !reason.trim()) return;
  try {
    await api("/api/reports", "POST", { call_id: lastCallId, reason: reason.trim() });
    alert("通報を受け付けました。ご協力ありがとうございます。");
  } catch (err) {
    alert(err.message);
  }
}

$("btn-report-call").onclick = reportPeer;
$("btn-report-survey").onclick = reportPeer;

$("btn-block-survey").onclick = async () => {
  if (!lastCallId) return;
  if (!confirm("この相手をブロックします。今後マッチングされなくなります。よろしいですか？")) return;
  try {
    await api("/api/blocks", "POST", { call_id: lastCallId });
    alert("ブロックしました。今後この相手とはマッチングされません。");
  } catch (err) {
    alert(err.message);
  }
};

// ---- マッチ成立の通知音 -----------------------------------------------------------
function playMatchSound() {
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ac.createOscillator();
    const gain = ac.createGain();
    osc.connect(gain);
    gain.connect(ac.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.start();
    osc.frequency.setValueAtTime(1175, ac.currentTime + 0.12);
    gain.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.45);
    osc.stop(ac.currentTime + 0.5);
  } catch { /* 音が出なくても支障なし */ }
}

// ---- カメラ・マイク・アバターのテスト ----------------------------------------------
let testStream = null;
let testLoopId = null;
let testAudioCtx = null;

function mediaConstraints() {
  const cam = localStorage.getItem("vm_cam");
  const mic = localStorage.getItem("vm_mic");
  return {
    video: cam ? { deviceId: { ideal: cam } } : true,
    audio: mic ? { deviceId: { ideal: mic } } : true,
  };
}

async function openDeviceTest() {
  $("device-error").textContent = "";
  $("test-avatar-name").textContent = AVATARS[avatarType].label;
  $("device-overlay").classList.remove("hidden");
  await startDeviceTest();
  await populateDeviceLists();
}

async function populateDeviceLists() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const fill = (sel, kind, storedKey) => {
    const stored = localStorage.getItem(storedKey) || "";
    const list = devices.filter((d) => d.kind === kind);
    sel.innerHTML = list
      .map((d, i) => `<option value="${d.deviceId}">${escapeHtml(d.label || `${kind === "videoinput" ? "カメラ" : "マイク"}${i + 1}`)}</option>`)
      .join("");
    if (list.some((d) => d.deviceId === stored)) sel.value = stored;
  };
  fill($("sel-cam"), "videoinput", "vm_cam");
  fill($("sel-mic"), "audioinput", "vm_mic");
}

async function startDeviceTest() {
  stopDeviceTest(false);
  try {
    testStream = await navigator.mediaDevices.getUserMedia(mediaConstraints());
  } catch {
    $("device-error").textContent = "カメラ・マイクを利用できませんでした。ブラウザの設定を確認してください。";
    return;
  }
  const avatarMode = $("test-avatar-mode").checked;
  $("test-video").classList.toggle("hidden", avatarMode);
  $("test-canvas").classList.toggle("hidden", !avatarMode);

  // マイクレベルメーター
  testAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = testAudioCtx.createAnalyser();
  analyser.fftSize = 256;
  testAudioCtx.createMediaStreamSource(testStream).connect(analyser);
  const buf = new Uint8Array(analyser.frequencyBinCount);

  if (avatarMode) {
    // アバターでプレビュー(通話と同じトラッキングパイプライン)
    const trackVideo = document.createElement("video");
    trackVideo.muted = true;
    trackVideo.playsInline = true;
    trackVideo.srcObject = new MediaStream(testStream.getVideoTracks());
    await trackVideo.play().catch(() => {});
    await loadFaceLandmarker();
    const ctx = $("test-canvas").getContext("2d");
    let lastTime = -1;
    const tick = () => {
      if (faceLandmarker && trackVideo.readyState >= 2 && trackVideo.currentTime !== lastTime) {
        lastTime = trackVideo.currentTime;
        try {
          updateFaceFromResult(faceLandmarker.detectForVideo(trackVideo, performance.now()));
        } catch {}
      }
      for (const k of Object.keys(faceCur)) faceCur[k] += (faceTgt[k] - faceCur[k]) * 0.35;
      const type = AVATARS[avatarType].mode === "toon" ? avatarType : "maru";
      drawAvatarOn(ctx, 512, 320, type, avatarColorRole(), faceCur);
      analyser.getByteFrequencyData(buf);
      const vol = buf.reduce((a, b) => a + b, 0) / buf.length / 255;
      $("mic-level").style.width = `${Math.min(100, vol * 300)}%`;
      testLoopId = requestAnimationFrame(tick);
    };
    tick();
  } else {
    $("test-video").srcObject = testStream;
    const tick = () => {
      analyser.getByteFrequencyData(buf);
      const vol = buf.reduce((a, b) => a + b, 0) / buf.length / 255;
      $("mic-level").style.width = `${Math.min(100, vol * 300)}%`;
      testLoopId = requestAnimationFrame(tick);
    };
    tick();
  }
}

function stopDeviceTest(hide = true) {
  if (testLoopId) { cancelAnimationFrame(testLoopId); testLoopId = null; }
  if (testStream) { testStream.getTracks().forEach((t) => t.stop()); testStream = null; }
  if (testAudioCtx) { testAudioCtx.close().catch(() => {}); testAudioCtx = null; }
  $("test-video").srcObject = null;
  $("mic-level").style.width = "0%";
  if (hide) $("device-overlay").classList.add("hidden");
}

$("btn-device-test").onclick = openDeviceTest;
$("btn-device-test2").onclick = openDeviceTest;
$("btn-device-close").onclick = () => stopDeviceTest(true);
$("test-avatar-mode").onchange = () => startDeviceTest();
$("sel-cam").onchange = () => {
  localStorage.setItem("vm_cam", $("sel-cam").value);
  startDeviceTest();
};
$("sel-mic").onchange = () => {
  localStorage.setItem("vm_mic", $("sel-mic").value);
  startDeviceTest();
};

// ---- システム管理画面(システム管理者のみ) ----------------------------------------
$("btn-sysadmin").onclick = () => showSysadmin();
$("btn-sysadmin-back").onclick = () => showLobby();

const roleLabels = {
  system_admin: "システム管理者",
  site_admin: "サイト管理者",
  moderator: "モデレータ",
  user: "一般",
};

/* システム管理画面のページ切替(サイト一覧 / サイト作成 / サイトの詳細) */
function showSysPage(name) {
  document.querySelectorAll("#screen-sysadmin .admin-page").forEach((el) => el.classList.add("hidden"));
  $(`sys-page-${name}`).classList.remove("hidden");
  document.querySelectorAll("#screen-sysadmin .sys-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.page === name);
  });
}
document.querySelectorAll("#screen-sysadmin .sys-tab").forEach((tab) => {
  tab.onclick = () => showSysPage(tab.dataset.page);
});

async function showSysadmin() {
  $("sysadmin-error").textContent = "";
  showScreen("sysadmin");
  showSysPage("sites");
  try {
    await loadSysSites();
  } catch (err) {
    $("sysadmin-error").textContent = err.message;
  }
}

async function loadSysSites() {
  const sites = await api("/api/sysadmin/sites");
  $("sys-site-rows").innerHTML = sites
    .map(
      (s) => `<tr>
        <td>${s.id}</td>
        <td class="nowrap">${escapeHtml(s.slug)}</td>
        <td class="nowrap"><strong>${escapeHtml(s.name)}</strong></td>
        <td class="nowrap">${s.is_main ? '<span class="role-tag speaker">メイン</span>' : "サブ"}</td>
        <td>${s.users}</td>
        <td class="admin-actions">
          <button class="btn-text" data-users="${s.id}" data-slug="${escapeHtml(s.slug)}">ユーザー</button>
          <button class="btn-text" data-settings="${s.id}" data-slug="${escapeHtml(s.slug)}">設定</button>
          ${s.is_main ? "" : `<button class="btn-text danger" data-delsite="${s.id}" data-slug="${escapeHtml(s.slug)}">削除</button>`}
        </td>
      </tr>`
    )
    .join("");

  const rows = $("sys-site-rows");
  rows.querySelectorAll("[data-users]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const users = await api(`/api/sysadmin/sites/${btn.dataset.users}/users`);
        showSysPage("detail");
        $("sys-users-site").textContent = `: ${btn.dataset.slug}`;
        $("sys-user-rows").innerHTML = users.length
          ? users
              .map(
                (u) => `<tr>
                  <td>${u.id}</td>
                  <td>${escapeHtml(u.username)}</td>
                  <td>${roleLabels[u.role] || u.role}</td>
                  <td>${u.session_count}</td>
                </tr>`
              )
              .join("")
          : '<tr><td colspan="4" class="empty-note">ユーザーがいません</td></tr>';
      } catch (err) {
        $("sysadmin-error").textContent = err.message;
      }
    };
  });
  rows.querySelectorAll("[data-settings]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const s = await api(`/api/sysadmin/sites/${btn.dataset.settings}/settings`);
        showSysPage("detail");
        $("sys-settings-site").textContent = `: ${btn.dataset.slug}`;
        const modes = [
          s.mode_toon ? "デフォルメ" : null,
          s.mode_real ? "リアル" : null,
          s.mode_still ? "静止画" : null,
          s.mode_camera ? "実映像" : null,
        ].filter(Boolean).join("・") || "なし";
        $("sys-settings-view").innerHTML = `
          <li><div class="a-title">セッション時間</div><div class="a-body">${s.session_minutes}分</div></li>
          <li><div class="a-title">新規登録</div><div class="a-body">${s.allow_registration ? "許可" : "停止中"}</div></li>
          <li><div class="a-title">マッチング</div><div class="a-body">${s.role_matching ? "話し手×聞き手" : "役割なし"}</div></li>
          <li><div class="a-title">表示</div><div class="a-body">${s.anonymous_mode ? "匿名(ランダムな呼び名)" : "実名(ユーザー名)"}</div></li>
          <li><div class="a-title">表示モード</div><div class="a-body">${modes}</div></li>
          <li><div class="a-title">ルーム機能</div><div class="a-body">${s.rooms_enabled ? "有効" : "無効"}</div></li>
          <li><div class="a-title">アンケート</div><div class="a-body">${s.survey_enabled ? escapeHtml(s.survey_question) : "なし"}</div></li>`;
      } catch (err) {
        $("sysadmin-error").textContent = err.message;
      }
    };
  });
  rows.querySelectorAll("[data-delsite]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm(`サイト「${btn.dataset.slug}」と所属ユーザー・データをすべて削除します。よろしいですか？`)) return;
      try {
        await api(`/api/sysadmin/sites/${btn.dataset.delsite}`, "DELETE");
        await loadSysSites();
      } catch (err) {
        $("sysadmin-error").textContent = err.message;
      }
    };
  });
}

$("sys-site-form").onsubmit = async (e) => {
  e.preventDefault();
  $("sysadmin-error").textContent = "";
  try {
    const data = await api("/api/sysadmin/sites", "POST", {
      slug: $("site-slug").value.trim(),
      name: $("site-name").value.trim(),
    });
    $("site-slug").value = "";
    $("site-name").value = "";
    const box = $("sys-site-created");
    box.classList.remove("hidden");
    box.innerHTML = `サイト「${escapeHtml(data.name)}」を作成しました。<br>
      サイト管理者: <strong>${escapeHtml(data.admin_username)}</strong><br>
      初期パスワード: <strong>${escapeHtml(data.initial_password)}</strong><br>
      <small>この情報は今だけ表示されます。サイト管理者に伝えてください(初回ログイン時にパスワード変更が必要です)。</small>`;
    await loadSysSites();
  } catch (err) {
    $("sysadmin-error").textContent = err.message;
  }
};

// ---- PWA(Service Worker登録) ---------------------------------------------------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    /* 登録できなくても通常のWebとして動作する */
  });
}

// ---- 初期化 -----------------------------------------------------------------
(async function init() {
  if (!token) {
    showScreen("auth");
    return;
  }
  try {
    const me = await api("/api/me");
    setLoggedIn(token, me.username, me.role, me.must_change_password);
  } catch {
    logout();
  }
})();
