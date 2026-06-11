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

const RTC_CONFIG = {
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};

// ---- ユーティリティ -----------------------------------------------------------
const $ = (id) => document.getElementById(id);

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
    case "queued":
      showScreen("waiting");
      break;

    case "matched":
      currentRoomId = msg.room_id;
      $("consent-peer").textContent = msg.peer_name;
      $("consent-status").textContent = "";
      $("btn-consent-ok").disabled = false;
      $("btn-consent-ng").disabled = false;
      showScreen("consent");
      break;

    case "call_start":
      isInitiator = msg.initiator;
      await startCall();
      break;

    case "peer_declined":
      // 相手が同意しなかった → 自動で再マッチング
      cleanupCall(false);
      wsSend({ type: "join_queue" });
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
$("btn-start-matching").onclick = async () => {
  $("lobby-error").textContent = "";
  try {
    if (!ws) await connectWS();
    wsSend({ type: "join_queue" });
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
    localStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  } catch (err) {
    $("consent-status").textContent = "";
    $("lobby-error").textContent =
      "カメラ・マイクを利用できませんでした。ブラウザの設定を確認してください。";
    wsSend({ type: "consent", accept: false });
    showLobby();
    return;
  }
  $("consent-status").textContent = "相手の同意を待っています…";
  wsSend({ type: "consent", accept: true });
};

$("btn-consent-ng").onclick = () => {
  wsSend({ type: "consent", accept: false });
  cleanupCall();
  showLobby();
};

// ---- WebRTC通話 -----------------------------------------------------------------
async function startCall() {
  showScreen("call");
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
  if (pc) { pc.close(); pc = null; }
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
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
  $("stat-waiting").textContent = stats.waiting;
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
