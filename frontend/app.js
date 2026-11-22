// Web Audio 8-bit Synthesizer for Retro Sound Effects
// _fogless_patched
let audioCtx = null;
let soundEnabled = true;

function getAudioCtx() {
  if (!audioCtx) {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (AudioContext) audioCtx = new AudioContext();
  }
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
  return audioCtx;
}

function playBeep(freq = 440, type = 'square', duration = 0.08) {
  if (!soundEnabled) return;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime);
    gain.gain.setValueAtTime(0.12, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + duration);
  } catch(e) {}
}

function playCoin() {
  if (!soundEnabled) return;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square';
    osc.frequency.setValueAtTime(987.77, ctx.currentTime); // B5
    osc.frequency.setValueAtTime(1318.51, ctx.currentTime + 0.08); // E6
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
}

function playPurr() {
  if (!soundEnabled) return;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    [523.25, 659.25, 783.99, 1046.50].forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(freq, ctx.currentTime + idx * 0.05);
      gain.gain.setValueAtTime(0.12, ctx.currentTime + idx * 0.05);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + idx * 0.05 + 0.1);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + idx * 0.05);
      osc.stop(ctx.currentTime + idx * 0.05 + 0.1);
    });
  } catch(e) {}
}

function playUndo() {
  if (!soundEnabled) return;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sawtooth';
    osc.frequency.setValueAtTime(400, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(150, ctx.currentTime + 0.2);
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.2);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.2);
  } catch(e) {}
}

// DOM Elements
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('terminal-input');
  const sendBtn = document.getElementById('send-btn');
  const micBtn = document.getElementById('mic-btn');
  const chatThread = document.getElementById('chat-thread');
  const balanceVal = document.getElementById('balance-val');
  const balancePct = null; // removed in Sanctuary layout
  const todayLoggedVal = document.getElementById('today-logged-val');
  const todayIncomeVal = document.getElementById('today-income-val');
  const todayGaugeCells = document.getElementById('today-gauge-cells');
  const todayPctVal = document.getElementById('today-pct-val');
  const headerSprite = document.getElementById('extreme-master-svg') || document.getElementById('mochi-master-svg');
  const pokeHeart = document.getElementById('poke-heart');
  const mochiWrap = document.getElementById('extreme-poke-trigger') || document.getElementById('mochi-poke-trigger');
  const btnA = document.getElementById('btn-a');
  const btnB = document.getElementById('btn-b');
  const soundIndicator = document.getElementById('sound-btn');
  const retroClock = document.getElementById('clock-display');
  const greetingTime = document.getElementById('greeting-time');

  // Slash spell drawer toggle
  const spellsToggle = document.getElementById('spells-toggle');
  const slashDrawer = document.getElementById('slash-suggest-drawer');
  const drawerCloseBtn = document.getElementById('drawer-close-btn');
  if (spellsToggle && slashDrawer) {
    spellsToggle.addEventListener('click', () => {
      playBeep(440);
      slashDrawer.classList.toggle('hidden');
    });
  }
  if (drawerCloseBtn && slashDrawer) {
    drawerCloseBtn.addEventListener('click', () => {
      playBeep(330);
      slashDrawer.classList.add('hidden');
    });
  }

  // Phone menu: Quick Actions (balances, export, backup, delete account) open as a full-screen sheet.
  const menuBtn = document.getElementById('menu-btn');
  const rightPanel = document.getElementById('right-panel');
  const panelClose = document.getElementById('panel-close');
  function setMenu(open) {
    if (!rightPanel) return;
    rightPanel.classList.toggle('open', open);
  }
  if (menuBtn) menuBtn.addEventListener('click', () => { playBeep(440); setMenu(true); });
  if (panelClose) panelClose.addEventListener('click', () => { playBeep(330); setMenu(false); });
  if (rightPanel) {
    rightPanel.addEventListener('click', (e) => {
      if (e.target.closest && e.target.closest('.panel-btn')) setMenu(false);
    });
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setMenu(false); });
  window.addEventListener('resize', () => { if (window.innerWidth >= 1024) setMenu(false); });

  let currentBalance = 0;
  let todaySpent = 0;
  const todayLimit = 120.00;
  let commandHistory = [];
  let historyIdx = -1;
  let mediaRecorder = null;
  let audioChunks = [];
  let isRecording = false;
  let lastMsgId = 0;            // newest chat_log id this page has seen (live replies report theirs)
  let oldestMsgId = 0;          // oldest loaded chat_log id: the cursor for "load earlier"
  let hasEarlier = false;
  let loadingEarlier = false;
  let syncing = false;
  let lastDayKey = null;        // day of the newest message on screen (drives the day separators)

  // Real Clock updating
  function updateClock() {
    const now = new Date();
    let h = now.getHours();
    const mins = String(now.getMinutes()).padStart(2, '0');
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const timeStr = `${String(h).padStart(2, '0')}:${mins} ${ampm}`;
    if (retroClock) retroClock.textContent = timeStr;
    if (greetingTime) greetingTime.textContent = timeStr;
  }
  updateClock();
  setInterval(updateClock, 10000);

  // Sound Toggle
  if (soundIndicator) {
    soundIndicator.addEventListener('click', () => {
      soundEnabled = !soundEnabled;
      if (soundEnabled) {
        soundIndicator.textContent = '♪ MIST LO-FI';
        soundIndicator.style.color = '#6EE7B7';
        playBeep(880);
      } else {
        soundIndicator.textContent = '✕ MUTED';
        soundIndicator.style.color = '#EF4444';
      }
    });
  }

  // Extreme Avatar Poke Interaction
  if (mochiWrap) {
    mochiWrap.addEventListener('click', () => {
      playPurr();
      if (headerSprite) {
        headerSprite.classList.remove('extreme-idle', 'mochi-idle');
        headerSprite.classList.add('extreme-jump');
      }
      if (pokeHeart) pokeHeart.style.display = 'block';
      setTimeout(() => {
        if (pokeHeart) pokeHeart.style.display = 'none';
        if (headerSprite) {
          headerSprite.classList.remove('extreme-jump', 'mochi-jump');
          headerSprite.classList.add('extreme-idle');
        }
      }, 1000);
    });
  }

  // Handheld A button
  if (btnA) {
    btnA.addEventListener('click', () => {
      playBeep(600);
      executeLog();
    });
  }

  // Handheld B button (Undo)
  if (btnB) {
    btnB.addEventListener('click', () => {
      playUndo();
      handleQuickUndo();
    });
  }

  // D-Pad navigation
  const dpadUp = document.getElementById('dpad-up');
  const dpadDown = document.getElementById('dpad-down');
  if (dpadUp) {
    dpadUp.addEventListener('click', () => {
      if (commandHistory.length > 0) {
        playBeep(350);
        if (historyIdx < commandHistory.length - 1) historyIdx++;
        input.value = commandHistory[commandHistory.length - 1 - historyIdx];
      }
    });
  }
  if (dpadDown) {
    dpadDown.addEventListener('click', () => {
      if (historyIdx > 0) {
        playBeep(350);
        historyIdx--;
        input.value = commandHistory[commandHistory.length - 1 - historyIdx];
      } else if (historyIdx === 0) {
        historyIdx = -1;
        input.value = '';
      }
    });
  }

  // Command directory chips & Quick Action panel buttons
  document.querySelectorAll('.cmd-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      playBeep(520);
      const cmd = chip.getAttribute('data-cmd') || chip.textContent.trim();
      if (cmd === '/export') {
        window.location.href = '/api/export';
      } else if (cmd === '/backup') {
        window.location.href = '/api/backup';
      } else if (cmd.startsWith('/')) {
        executeCommandText(cmd);
      } else {
        input.value = cmd + ' ';
        input.focus();
      }
    });
  });

  // Update balance & budget pace gauge UI
  function updateBalanceDisplay(newBalance) {
    currentBalance = newBalance;
    if (balanceVal) balanceVal.textContent = currentBalance.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function updateTodayPaceDisplay(newTodaySpent) {
    todaySpent = newTodaySpent;
    if (todayLoggedVal) todayLoggedVal.textContent = todaySpent.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const pct = Math.min(100, Math.round((todaySpent / todayLimit) * 100));
    if (todayPctVal) todayPctVal.textContent = `${pct}%`;

    if (todayGaugeCells) {
      const filledCells = Math.min(6, Math.round((todaySpent / todayLimit) * 6));
      todayGaugeCells.innerHTML = '';
      for (let i = 0; i < 6; i++) {
        const cell = document.createElement('div');
        cell.className = i < filledCells
          ? (i >= 4 ? 'pace-cell warn' : 'pace-cell filled')
          : 'pace-cell';
        todayGaugeCells.appendChild(cell);
      }
    }
  }

  function updateTodayIncomeDisplay(newIncome) {
    if (todayIncomeVal) todayIncomeVal.textContent = newIncome.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // Every server reply that carries totals goes through here.
  function applyTotals(d) {
    if (!d) return;
    if (d.balance !== undefined) updateBalanceDisplay(d.balance);
    if (d.today_spent !== undefined) updateTodayPaceDisplay(d.today_spent);
    if (d.today_income !== undefined) updateTodayIncomeDisplay(d.today_income);
  }
  function noteChatId(id) {
    const n = Number(id);
    if (Number.isInteger(n) && n > lastMsgId) lastMsgId = n;
  }

  // Voice Note Recording
  if (micBtn) {
    micBtn.addEventListener('click', async () => {
      if (!isRecording) {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          mediaRecorder = new MediaRecorder(stream);
          audioChunks = [];
          mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
          mediaRecorder.onstop = async () => {
            // Label the file with what the browser REALLY recorded (Chrome=webm, Firefox=ogg, Safari=mp4)
            const mime = (mediaRecorder && mediaRecorder.mimeType) || 'audio/webm';
            const ext = mime.includes('ogg') ? 'ogg' : (mime.includes('mp4') ? 'm4a' : 'webm');
            const audioBlob = new Blob(audioChunks, { type: mime });
            uploadVoiceRecording(audioBlob, ext);
          };
          mediaRecorder.start();
          isRecording = true;
          micBtn.classList.add('recording-pulse');
          micBtn.textContent = '⏹ STOP';
          playBeep(880);
        } catch(err) {
          alert("Microphone access not granted or unavailable. You can type voice text directly!");
        }
      } else {
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
          mediaRecorder.stop();
          mediaRecorder.stream.getTracks().forEach(track => track.stop());
        }
        isRecording = false;
        micBtn.classList.remove('recording-pulse');
        micBtn.textContent = '🎙 REC';
        playBeep(440);
      }
    });
  }

  async function uploadVoiceRecording(blob, ext = 'webm') {
    const cassetteBubble = document.createElement('div');
    cassetteBubble.className = 'msg-user';
    cassetteBubble.innerHTML = `
      <div class="bubble-rose">
        <div class="bubble-meta">
          <span>VOICE MEMO · CASSETTE ${String(Math.floor(Math.random()*90)+10)}</span>
          <span>JUST NOW</span>
        </div>
        <div style="background:#FFF0ED;border:1px solid var(--rose-border);border-radius:8px;padding:8px 12px;display:flex;align-items:center;gap:12px;">
          <button style="width:32px;height:32px;background:var(--rose-dark);color:white;border:none;border-radius:6px;display:flex;align-items:center;justify-content:center;cursor:pointer;" class="play-cassette-btn">
            ▶
          </button>
          <div style="display:flex;align-items:flex-end;gap:4px;height:24px;flex:1;background:#FCE4DE;border-radius:4px;padding:4px 8px;">
            <div style="width:4px;height:60%;background:var(--rose-dark);border-radius:2px;"></div>
            <div style="width:4px;height:100%;background:var(--rose-dark);border-radius:2px;"></div>
            <div style="width:4px;height:40%;background:var(--rose-dark);border-radius:2px;"></div>
            <div style="width:4px;height:80%;background:var(--rose-dark);border-radius:2px;"></div>
            <div style="width:4px;height:50%;background:var(--rose-dark);border-radius:2px;"></div>
          </div>
          <div style="font-family:'Press Start 2P',monospace;font-size:7px;color:var(--rose-dark);">AUDIO</div>
        </div>
      </div>
    `;
    pushMsg(cassetteBubble);

    const formData = new FormData();
    formData.append('audio', blob, `voice.${ext}`);
    try {
      const res = await fetch('/api/voice', { method: 'POST', body: formData, credentials: 'same-origin' });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Voice upload failed (HTTP ${res.status}). Nothing was logged.`);
        return;
      }
      noteChatId(data.chat_id);
      if (data.ok) {
        playCoin();
        applyTotals(data);
        renderCompanionBubble(data.message_html || plainToHtml(data.text || ''), data.tx_id, data.category, data.payment_method);
      } else {
        renderCompanionBubble(`⚠ ${escapeHtml(data.text || 'Voice note could not be processed.')}`);
      }
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — the voice note may not have been processed. Check /history before retrying.`);
    }
  }

  // Server Communication & Logic Execution

  // ── Chat retention ──────────────────────────────────────────────────────────────
  // The whole conversation lives in the database. The page shows the newest HISTORY_PAGE messages, fetches
  // older ones as you scroll up (or tap the pill), marks every new day with a separator, shows each message's
  // real time, and catches up on anything logged elsewhere (e.g. the home-screen widget) when you come back.
  const HISTORY_PAGE = 100;
  const pad2 = (n) => String(n).padStart(2, '0');
  function dayKey(d) { return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; }
  function keyToDate(k) { const p = k.split('-').map(Number); return new Date(p[0], p[1] - 1, p[2]); }
  function fmtClock(d) {
    let h = d.getHours();
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return `${pad2(h)}:${pad2(d.getMinutes())} ${ampm}`;
  }
  function dayLabel(key) {
    const d = keyToDate(key);
    const now = new Date();
    const diff = Math.round((keyToDate(dayKey(now)) - d) / 86400000);
    if (diff === 0) return 'Today';
    if (diff === 1) return 'Yesterday';
    const base = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    return d.getFullYear() === now.getFullYear() ? base : `${base} ${d.getFullYear()}`;
  }
  function makeSep(key) {
    const el = document.createElement('div');
    el.className = 'fl-day-sep';
    el.dataset.day = key;
    el.innerHTML = `<span>${escapeHtml(dayLabel(key))}</span>`;
    return el;
  }
  function refreshDayLabels() {
    chatThread.querySelectorAll('.fl-day-sep').forEach((el) => {
      const span = el.firstElementChild;
      if (span && el.dataset.day) span.textContent = dayLabel(el.dataset.day);
    });
  }
  function firstFlowEl() {
    return Array.from(chatThread.children).find((el) => el.matches('.msg-assistant, .msg-user, .fl-day-sep'));
  }
  // A live message: open a new day separator first if the day changed since the last one.
  function pushMsg(el) {
    const key = dayKey(new Date());
    if (key !== lastDayKey) { chatThread.appendChild(makeSep(key)); lastDayKey = key; }
    el.dataset.day = key;
    chatThread.appendChild(el);
    chatThread.scrollTop = chatThread.scrollHeight;
  }

  function buildUserBubble(text, label) {
    const bubble = document.createElement('div');
    bubble.className = 'msg-user';
    bubble.innerHTML = `
      <div class="bubble-rose">
        <div class="bubble-meta">
          <span>HERO</span>
          <span>${escapeHtml(label)}</span>
        </div>
        <p style="font-weight:600">${escapeHtml(text)}</p>
      </div>
    `;
    return bubble;
  }
  function buildHistoryNode(msg, when) {
    const label = when ? fmtClock(when) : '';
    if (msg.role === 'user') return buildUserBubble(msg.content, label);
    const c = String(msg.content == null ? '' : msg.content);
    return buildCompanionBubble(c.trimStart().startsWith('<') ? c : c.replace(/\r?\n/g, '<br>'), null, null, null, when || false);
  }
  // Messages (oldest first) -> DOM fragment with a day separator wherever the day changes.
  function buildBatch(messages, prevKey) {
    const frag = document.createDocumentFragment();
    let lastKey = prevKey || null;
    messages.forEach((msg) => {
      let when = msg.created_at ? new Date(msg.created_at) : null;
      if (when && isNaN(when)) when = null;
      const key = when ? dayKey(when) : lastKey;
      if (key && key !== lastKey) frag.appendChild(makeSep(key));
      const node = buildHistoryNode(msg, when);
      if (key) { node.dataset.day = key; lastKey = key; }
      frag.appendChild(node);
    });
    return { frag, lastKey };
  }
  // Put older messages above what is on screen, keeping the day separators consistent at the seam.
  function prependMessages(messages) {
    const { frag, lastKey } = buildBatch(messages, null);
    const anchor = firstFlowEl();
    if (anchor) {
      if (anchor.classList.contains('fl-day-sep')) {
        if (anchor.dataset.day === lastKey) anchor.remove();           // the day simply continues
      } else if (anchor.dataset.day && anchor.dataset.day !== lastKey) {
        frag.appendChild(makeSep(anchor.dataset.day));                 // live message of a newer day
      }
    }
    chatThread.insertBefore(frag, firstFlowEl() || null);
    return lastKey;
  }
  function trackIds(rows) {
    rows.forEach((m) => {
      const id = Number(m.id);
      if (!Number.isInteger(id) || id <= 0) return;
      lastMsgId = Math.max(lastMsgId, id);
      oldestMsgId = oldestMsgId ? Math.min(oldestMsgId, id) : id;
    });
  }
  async function fetchHistory(params) {
    const res = await fetch('/api/history?' + new URLSearchParams(params), { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) throw new Error(`history fetch failed: ${res.status}`);
    const rows = await res.json();
    return Array.isArray(rows) ? rows : [];
  }

  const earlierObserver = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting) && chatThread.scrollTop < 160) loadEarlier();
      }, { root: chatThread, rootMargin: '160px 0px 0px 0px' })
    : null;
  function setEarlierControl() {
    let el = document.getElementById('load-earlier');
    if (!hasEarlier) {
      if (el && el.tagName === 'BUTTON') {
        const done = document.createElement('div');
        done.id = 'load-earlier';
        done.className = 'fl-earlier-done';
        done.textContent = '— start of your history —';
        if (earlierObserver) earlierObserver.unobserve(el);
        el.replaceWith(done);
      }
      return;
    }
    if (!el) {
      el = document.createElement('button');
      el.id = 'load-earlier';
      el.type = 'button';
      el.className = 'fl-earlier';
      el.addEventListener('click', () => loadEarlier());
      chatThread.insertBefore(el, firstFlowEl() || null);
      if (earlierObserver) earlierObserver.observe(el);
    }
    el.disabled = false;
    el.textContent = '↑ Load earlier messages';
  }
  async function loadEarlier() {
    if (loadingEarlier || !hasEarlier || !oldestMsgId) return;
    loadingEarlier = true;
    const btn = document.getElementById('load-earlier');
    if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
    try {
      const rows = await fetchHistory({ limit: HISTORY_PAGE, before: oldestMsgId });
      const prevH = chatThread.scrollHeight;
      const prevTop = chatThread.scrollTop;
      if (rows.length) { prependMessages(rows); trackIds(rows); }
      hasEarlier = rows.length >= HISTORY_PAGE;
      chatThread.scrollTop = prevTop + (chatThread.scrollHeight - prevH);   // keep reading where you were
    } catch (err) {
      console.error('Failed to load earlier messages:', err);
    } finally {
      loadingEarlier = false;
      setEarlierControl();
    }
  }

  // Restore persisted chat history on page load
  async function loadChatHistory() {
    try {
      const rows = await fetchHistory({ limit: HISTORY_PAGE });
      if (!rows.length) return;
      const greeting = chatThread.querySelector('[data-greeting]');
      if (greeting) greeting.remove();                       // real history replaces the default greeting
      const hadLive = !!firstFlowEl();                       // you may have typed before this arrived
      const lastKey = prependMessages(rows);
      trackIds(rows);
      if (!hadLive && lastKey) lastDayKey = lastKey;
      hasEarlier = rows.length >= HISTORY_PAGE;
      setEarlierControl();
      chatThread.scrollTop = chatThread.scrollHeight;
    } catch (err) {
      console.error('Failed to load chat history:', err);
    }
  }
  loadChatHistory();

  // Anything logged since this page last looked (home-screen widget, another device): append it.
  async function syncNewMessages() {
    if (syncing) return;
    syncing = true;
    try {
      const rows = await fetchHistory({ after: lastMsgId, limit: HISTORY_PAGE });
      if (!rows.length) return;
      const greeting = chatThread.querySelector('[data-greeting]');
      if (greeting) greeting.remove();
      const nearBottom = chatThread.scrollHeight - chatThread.scrollTop - chatThread.clientHeight < 160;
      const { frag, lastKey } = buildBatch(rows, lastDayKey);
      chatThread.appendChild(frag);
      if (lastKey) lastDayKey = lastKey;
      trackIds(rows);
      if (nearBottom) chatThread.scrollTop = chatThread.scrollHeight;
    } catch (err) {
      console.error('Chat sync failed:', err);
    } finally {
      syncing = false;
    }
  }
  let hiddenAt = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { hiddenAt = Date.now(); return; }
    if (hiddenAt && Date.now() - hiddenAt < 4000) return;    // a quick app-switch is not worth a refresh
    refreshDayLabels();
    fetchInitialState(true);
    syncNewMessages();
  });

  async function fetchInitialState(quiet = false) {
    try {
      const res = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' });
      const data = await res.json();
      if (res.ok && data.ok) {
        applyTotals(data);
        return;
      }
      throw new Error((data && data.text) || `HTTP ${res.status}`);
    } catch(e) {
      console.error('Could not load state:', e);
      if (quiet) return;                       // a refresh that failed keeps the numbers already on screen
      // Never show made-up numbers: show that we don't know.
      if (balanceVal) balanceVal.textContent = '—';
      if (todayLoggedVal) todayLoggedVal.textContent = '—';
      if (todayIncomeVal) todayIncomeVal.textContent = '—';
      if (todayPctVal) todayPctVal.textContent = '–';
    }
  }
  fetchInitialState();

  async function executeCommandText(text) {
    pushMsg(buildUserBubble(text, 'JUST NOW'));

    if (headerSprite) {
      headerSprite.classList.remove('extreme-idle', 'mochi-idle');
      headerSprite.classList.add('extreme-jump');
      setTimeout(() => {
        headerSprite.classList.remove('extreme-jump', 'mochi-jump');
        headerSprite.classList.add('extreme-idle');
      }, 700);
    }

    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        // The server answered with something that is not JSON (crash / gateway error). Say so; never fake success.
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}). I can't confirm "${escapeHtml(text)}" was saved — check /history before retrying, or you may log it twice.`);
        return;
      }
      noteChatId(data.chat_id);
      if (data.ok) {
        playCoin();
        applyTotals(data);
        renderCompanionBubble(data.html || plainToHtml(data.text || ''), data.tx_id, data.category, data.payment_method);
      } else {
        renderCompanionBubble(plainToHtml(data.text || "Could not execute spell."));
      }
    } catch(e) {
      // Network failure: the request may or may not have reached the server.
      renderCompanionBubble(`⚠ Connection error — "${escapeHtml(text)}" may or may not have been saved. Check /history before retrying.`);
    }
  }

  function executeLog() {
    const text = input.value.trim();
    if (!text) return;
    commandHistory.push(text);
    historyIdx = -1;
    input.value = '';
    input.style.height = 'auto';
    executeCommandText(text);
  }

  if (sendBtn) sendBtn.addEventListener('click', executeLog);
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        executeLog();
      }
    });

    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
  }

  // `when`: a Date for a restored message, false for "unknown", omitted for "now".
  function buildCompanionBubble(htmlContent, txId = null, category = null, paymentMethod = null, when = null) {
    const bubble = document.createElement('div');
    bubble.className = 'msg-assistant';
    
    let inlineMenuHtml = '';
    txId = Number.isInteger(Number(txId)) && Number(txId) > 0 ? Number(txId) : null;  // only ever a plain number inside onclick
    if (txId) {
      const catLabel = escapeHtml(category || 'other');
      const payLabel = escapeHtml(paymentMethod || 'cash (default)');
      inlineMenuHtml = `
        <div style="background:#FAF8F3;border:1px solid var(--border);border-radius:6px;padding:8px;margin-top:8px;font-size:12px;">
          <div style="display:flex;align-items:center;justify-content:space-between;font-family:'Press Start 2P',monospace;font-size:6.5px;color:var(--sage);margin-bottom:6px;">
            <span>COMMAND: EDIT RECORD</span>
            <span style="color:var(--ink-muted);">PRESS A</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;">
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:var(--ink);" onclick="window.changeCategory(${txId})">
              <span style="color:var(--sage);font-weight:bold;margin-right:6px;">▶</span>
              <span>Category: <strong style="color:var(--sage-dark);">${catLabel}</strong></span>
            </button>
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:var(--ink);" onclick="window.changePayment(${txId})">
              <span style="color:var(--sage);font-weight:bold;margin-right:6px;">▶</span>
              <span>Payment: <strong style="color:var(--rose-dark);">${payLabel}</strong></span>
            </button>
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:#DC2626;" onclick="window.undoTxId(${txId})">
              <span style="color:#DC2626;font-weight:bold;margin-right:6px;">▶</span>
              <span style="font-weight:bold;">Cast Undo (Reverse #${txId})</span>
            </button>
          </div>
        </div>
      `;
    }

    const timeStr = when === false ? '' : fmtClock(when || new Date());

    bubble.innerHTML = `
      <div class="bubble-icon">
        <svg width="18" height="18" viewBox="0 0 16 16" fill="none">
          <rect x="7" y="2"  width="2" height="2" fill="#F472B6"/>
          <rect x="4" y="5"  width="2" height="2" fill="#F472B6"/>
          <rect x="10" y="5" width="2" height="2" fill="#F472B6"/>
          <rect x="7" y="8"  width="2" height="2" fill="#F472B6"/>
          <rect x="7" y="5"  width="2" height="2" fill="#FBBF24"/>
          <rect x="7" y="10" width="2" height="5" fill="#4ADE80"/>
          <rect x="5" y="11" width="2" height="2" fill="#22C55E"/>
        </svg>
      </div>
      <div class="bubble-sage">
        <div class="bubble-meta">
          <span>EXTREME · MEADOW GUIDE</span>
          <span>${timeStr}</span>
        </div>
        <div>${htmlContent}</div>
        ${inlineMenuHtml}
      </div>
    `;
    return bubble;
  }

  function renderCompanionBubble(htmlContent, txId = null, category = null, paymentMethod = null) {
    pushMsg(buildCompanionBubble(htmlContent, txId, category, paymentMethod, null));
  }

  window.handleQuickUndo = async function() {
    playUndo();
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'undo' }),
        credentials: 'same-origin',
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) — undo may not have gone through. Check /history.`);
        return;
      }
      if (data.ok) applyTotals(data);
      renderCompanionBubble(data.html || escapeHtml(data.text || '') || `⚔ SPELL: REVERT EXECUTED. Restored GP to the treasury purse.`);
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — undo did NOT go through. Balance unchanged. Try again.`);
    }
  };

  window.undoTxId = async function(txId) {
    playUndo();
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', tx_id: txId })
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) — #${txId} was NOT confirmed removed. Check /history.`);
        return;
      }
      if (data.ok) applyTotals(data);
      renderCompanionBubble(escapeHtml(data.text || (data.ok ? `Removed transaction #${txId}.` : `Could not remove #${txId}.`)));
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — #${txId} was NOT confirmed removed. Check /history.`);
    }
  };
  window.quickSetBalance = async function(method) {
    const label = String(method || 'cash').toUpperCase();
    const raw = prompt(`Enter correct ${label} balance (₹):`);
    if (raw === null) return;
    const amt = parseFloat(String(raw).replace(/[^0-9.]/g, ''));
    if (!Number.isFinite(amt) || amt < 0) {
      alert('Enter a valid non-negative number.');
      return;
    }
    playBeep(520);
    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: `/balance ${amt} ${method}` }),
        credentials: 'same-origin',
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) — ${label} balance NOT confirmed changed. Check /balance.`);
        return;
      }
      if (data.ok) {
        playCoin();
        applyTotals(data);
        renderCompanionBubble(data.html || escapeHtml(data.text || `${label} balance set to ₹${amt.toFixed(2)}.`));
      } else {
        renderCompanionBubble(`⚠ ${escapeHtml(data.text || `Could not set ${label} balance.`)}`);
      }
    } catch (e) {
      renderCompanionBubble(`⚠ Connection error — ${label} balance may not have changed. Check /balance.`);
    }
  };

  window.changeCategory = async function(txId) {
    const newCat = prompt("New category.\nExpense: food, groceries, transport, bills, shopping, entertainment, health, subscriptions, rent, education, travel, fitness, other\nIncome: salary, freelance, gift, refund, other", "groceries");
    if (!newCat) return;
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_category', tx_id: txId, category: newCat })
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) — category for #${txId} NOT confirmed changed.`);
        return;
      }
      renderCompanionBubble(data.ok ? `Updated category for #${txId} to <strong>${escapeHtml(data.category || newCat)}</strong>.` : `⚠ ${escapeHtml(data.text || 'Category not changed.')}`);
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — category for #${txId} NOT confirmed changed.`);
    }
  };

  window.changePayment = async function(txId) {
    const newPay = prompt("Enter payment method (cash, upi, card, netbanking):", "upi");
    if (!newPay) return;
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_payment', tx_id: txId, payment_method: newPay })
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) — payment method for #${txId} NOT confirmed changed.`);
        return;
      }
      renderCompanionBubble(data.ok ? `Updated payment method for #${txId} to <strong>${escapeHtml(data.payment_method || newPay)}</strong>.` : `⚠ ${escapeHtml(data.text || 'Payment method not changed.')}`);
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — payment method for #${txId} NOT confirmed changed.`);
    }
  };
  // ── Stats / History navigator ───────────────────────────────────────────────────
  // The cards the server sends carry their own controls (data-fl-*). A tap swaps that one card in place
  // through /api/browse: no new chat messages, no page reload, no typing.
  function showCardError(card, text) {
    const old = card.querySelector('.fl-err');
    if (old) old.remove();
    const el = document.createElement('div');
    el.className = 'fl-err';
    el.textContent = '⚠ ' + text;
    card.appendChild(el);
  }
  async function flBrowse(card, view, range, limit) {
    const ticket = (card._flTicket || 0) + 1;     // a slower, older answer must never overwrite a newer tap
    card._flTicket = ticket;
    card.classList.add('fl-loading');
    const params = new URLSearchParams({ view: view || 'stats', range: range || 'month' });
    if (view === 'history' && limit) params.set('limit', limit);
    try {
      const res = await fetch('/api/browse?' + params, { credentials: 'same-origin', cache: 'no-store' });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (card._flTicket !== ticket || !card.isConnected) return;
      if (data && data.ok && data.html) {
        const holder = document.createElement('div');
        holder.innerHTML = data.html;
        const fresh = holder.firstElementChild;
        if (fresh) { fresh._flTicket = ticket; card.replaceWith(fresh); return; }
      }
      card.classList.remove('fl-loading');
      showCardError(card, (data && data.text) || `Could not load (HTTP ${res.status}).`);
    } catch (e) {
      if (card._flTicket !== ticket) return;
      card.classList.remove('fl-loading');
      showCardError(card, 'Connection error — try again.');
    }
  }
  function openPicker(wrap) {
    const inp = wrap.querySelector('.fl-pick');
    const card = wrap.closest('.fl-browse');
    if (!inp || !card) return;
    if (inp.type === 'text') {                    // this browser has no date / month picker: ask in words
      const raw = prompt(inp.dataset.flPick === 'month'
        ? 'Which month? (2026-10, oct, oct 2025)' : 'Which day? (2026-11-20, 20/11/2026, 14 nov)');
      if (raw && raw.trim()) flBrowse(card, inp.dataset.flView, raw.trim());
      return;
    }
    try { if (typeof inp.showPicker === 'function') { inp.showPicker(); return; } } catch (_) { /* fall through */ }
    inp.focus();
    inp.click();
  }
  chatThread.addEventListener('click', (e) => {
    const wrap = e.target.closest('.fl-pick-wrap');
    if (wrap) { openPicker(wrap); return; }
    const btn = e.target.closest('button[data-fl-range]');
    if (!btn || btn.disabled) return;
    const card = btn.closest('.fl-browse');
    if (!card) return;
    playBeep(520);
    flBrowse(card, btn.dataset.flView || card.dataset.flView, btn.dataset.flRange, btn.dataset.flLimit);
  });
  chatThread.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('fl-pick-wrap')) {
      e.preventDefault();
      openPicker(e.target);
    }
  });
  chatThread.addEventListener('change', (e) => {
    const inp = e.target.closest && e.target.closest('.fl-pick');
    if (!inp || !inp.value) return;
    const card = inp.closest('.fl-browse');
    if (card) flBrowse(card, inp.dataset.flView || card.dataset.flView, inp.value);
  });

  async function _runReadOnlyCommand(cmd, label) {
    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cmd }),
        credentials: 'same-origin',
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`⚠ Server error (HTTP ${res.status}) while loading ${label}.`);
        return;
      }
      renderCompanionBubble(data.ok ? (data.html || plainToHtml(data.text || '')) : plainToHtml(data.text || `Could not load ${label}.`));
    } catch(e) {
      renderCompanionBubble(`⚠ Connection error — could not load ${label}.`);
    }
  }

  async function fetchAndDisplayStats() { return _runReadOnlyCommand('/stats', 'stats'); }
  async function fetchAndDisplayBalance() { return _runReadOnlyCommand('/balance', 'balances'); }
  async function fetchAndDisplayHistory() { return _runReadOnlyCommand('/history', 'history'); }
  // Plain-text replies (e.g. /aliases) keep their line breaks; server-built HTML is left alone.
  function plainToHtml(str) {
    return escapeHtml(str).replace(/\r?\n/g, '<br>');
  }

  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
});
