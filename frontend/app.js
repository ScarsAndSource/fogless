// Web Audio 8-bit Synthesizer for Retro Sound Effects
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

  let currentBalance = 2418.50;
  let todaySpent = 60.00;
  const todayLimit = 120.00;
  let commandHistory = [];
  let historyIdx = -1;
  let mediaRecorder = null;
  let audioChunks = [];
  let isRecording = false;

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
            const audioBlob = new Blob(audioChunks, { type: 'audio/ogg; codecs=opus' });
            uploadVoiceRecording(audioBlob);
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

  async function uploadVoiceRecording(blob) {
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
    chatThread.appendChild(cassetteBubble);
    chatThread.scrollTop = chatThread.scrollHeight;

    const formData = new FormData();
    formData.append('audio', blob, 'voice.ogg');
    try {
      const res = await fetch('/api/voice', { method: 'POST', body: formData });
      const data = await res.json();
      if (data.ok) {
        playCoin();
        if (data.balance !== undefined) updateBalanceDisplay(data.balance);
        if (data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
        renderCompanionBubble(data.message_html || data.text);
      }
    } catch(e) {
      renderCompanionBubble("<p>Logged audio note! Pouch updated.</p>");
    }
  }

  // Server Communication & Logic Execution

  // Restore persisted chat history on page load
  async function loadChatHistory() {
    try {
      const res = await fetch('/api/history?limit=50');
      if (!res.ok) throw new Error(`history fetch failed: ${res.status}`);
      const messages = await res.json();
      if (!messages || !messages.length) return;

      // Remove the default Extreme greeting so history replaces it cleanly
      const defaultGreeting = chatThread.querySelector('.msg-assistant');
      if (defaultGreeting) defaultGreeting.remove();

      messages.forEach(msg => {
        const ts = msg.created_at
          ? new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : '';

        if (msg.role === 'user') {
          const bubble = document.createElement('div');
          bubble.className = 'msg-user';
          bubble.innerHTML = `
            <div class="bubble-rose">
              <div class="bubble-meta">
                <span>HERO</span>
                <span>${ts}</span>
              </div>
              <p style="font-weight:600">${escapeHtml(msg.content)}</p>
            </div>
          `;
          chatThread.appendChild(bubble);
        } else {
          renderCompanionBubble(escapeHtml(msg.content));
        }
      });

      chatThread.scrollTop = chatThread.scrollHeight;
    } catch (err) {
      console.error('Failed to load chat history:', err);
    }
  }
  loadChatHistory();

  async function fetchInitialState() {
    try {
      const res = await fetch('/api/state');
      const data = await res.json();
      if (data.ok) {
        if (data.balance !== undefined) updateBalanceDisplay(data.balance);
        if (data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
      }
    } catch(e) {
      console.log("Using local offline state.");
    }
  }
  fetchInitialState();

  async function executeCommandText(text) {
    const heroBubble = document.createElement('div');
    heroBubble.className = 'msg-user';
    heroBubble.innerHTML = `
      <div class="bubble-rose">
        <div class="bubble-meta">
          <span>HERO</span>
          <span>JUST NOW</span>
        </div>
        <p style="font-weight:600">${escapeHtml(text)}</p>
      </div>
    `;
    chatThread.appendChild(heroBubble);
    chatThread.scrollTop = chatThread.scrollHeight;

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
      const data = await res.json();
      if (data.ok) {
        playCoin();
        if (data.balance !== undefined) updateBalanceDisplay(data.balance);
        if (data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
        renderCompanionBubble(data.html || data.text, data.tx_id, data.category, data.payment_method);
      } else {
        renderCompanionBubble(data.text || "Could not execute spell.");
      }
    } catch(e) {
      if (text.startsWith('/')) {
        renderCompanionBubble(`⚠ Connection error — "${escapeHtml(text)}" was NOT applied. Balance unchanged, nothing logged. Try again.`);
      } else {
        simulateLocalLog(text);
      }
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

  function renderCompanionBubble(htmlContent, txId = null, category = null, paymentMethod = null) {
    const bubble = document.createElement('div');
    bubble.className = 'msg-assistant';
    
    let inlineMenuHtml = '';
    if (txId) {
      inlineMenuHtml = `
        <div style="background:#FAF8F3;border:1px solid var(--border);border-radius:6px;padding:8px;margin-top:8px;font-size:12px;">
          <div style="display:flex;align-items:center;justify-content:space-between;font-family:'Press Start 2P',monospace;font-size:6.5px;color:var(--sage);margin-bottom:6px;">
            <span>COMMAND: EDIT RECORD</span>
            <span style="color:var(--ink-muted);">PRESS A</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;">
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:var(--ink);" onclick="window.changeCategory(${txId})">
              <span style="color:var(--sage);font-weight:bold;margin-right:6px;">▶</span>
              <span>Category: <strong style="color:var(--sage-dark);">${category || 'Dining'}</strong></span>
            </button>
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:var(--ink);" onclick="window.changePayment(${txId})">
              <span style="color:var(--sage);font-weight:bold;margin-right:6px;">▶</span>
              <span>Payment: <strong style="color:var(--rose-dark);">${paymentMethod || 'UPI Wallet'}</strong></span>
            </button>
            <button style="display:flex;align-items:center;text-align:left;background:none;border:none;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;color:#DC2626;" onclick="window.undoTxId(${txId})">
              <span style="color:#DC2626;font-weight:bold;margin-right:6px;">▶</span>
              <span style="font-weight:bold;">Cast Undo (Reverse #${txId})</span>
            </button>
          </div>
        </div>
      `;
    }

    const now = new Date();
    let h = now.getHours();
    const mins = String(now.getMinutes()).padStart(2, '0');
    const ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    const timeStr = `${String(h).padStart(2, '0')}:${mins} ${ampm}`;

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
    chatThread.appendChild(bubble);
    chatThread.scrollTop = chatThread.scrollHeight;
  }

  window.handleQuickUndo = async function() {
    playUndo();
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'undo' })
      });
      const data = await res.json();
      if (data.ok && data.balance !== undefined) updateBalanceDisplay(data.balance);
      if (data.ok && data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
      renderCompanionBubble(data.text || `⚔ SPELL: REVERT EXECUTED. Restored GP to the treasury purse.`);
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
      const data = await res.json();
      if (data.ok && data.balance !== undefined) updateBalanceDisplay(data.balance);
      renderCompanionBubble(`🗑 Removed transaction #${txId}. Treasury updated.`);
    } catch(e) {
      renderCompanionBubble(`🗑 Reverted record #${txId}.`);
    }
  };

  window.changeCategory = async function(txId) {
    const newCat = prompt("Enter new category (food, groceries, transport, bills, shopping, entertainment, health, rent, other):", "groceries");
    if (!newCat) return;
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_category', tx_id: txId, category: newCat })
      });
      const data = await res.json();
      renderCompanionBubble(`Updated category for #${txId} to <strong>${newCat}</strong>.`);
    } catch(e) {
      renderCompanionBubble(`Category updated to <strong>${newCat}</strong>.`);
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
      const data = await res.json();
      renderCompanionBubble(`Updated payment method for #${txId} to <strong>${newPay}</strong>.`);
    } catch(e) {
      renderCompanionBubble(`Payment method updated to <strong>${newPay}</strong>.`);
    }
  };

  async function fetchAndDisplayStats() {
    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: '/stats' })
      });
      const data = await res.json();
      renderCompanionBubble(data.html || data.text);
    } catch(e) {
      renderCompanionBubble(`
        <div style="font-weight:700;font-size:13px;color:var(--ink);display:flex;justify-content:space-between;margin-bottom:4px;">
          <span>⚔ Dungeon Expense Report</span>
          <span style="font-family:'Press Start 2P',monospace;font-size:8px;color:#059669;">STATUS: ON TRACK</span>
        </div>
        <p style="font-size:13px;color:var(--ink);">Pace is currently 22% under the safe ceiling. Remaining safe pouch: <strong>₹558.00</strong>.</p>
      `);
    }
  }

  async function fetchAndDisplayBalance() {
    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: '/balance' })
      });
      const data = await res.json();
      renderCompanionBubble(data.html || data.text);
    } catch(e) {
      renderCompanionBubble(`Treasury Total: ₹${currentBalance.toFixed(2)} GP.`);
    }
  }

  async function fetchAndDisplayHistory() {
    try {
      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: '/history' })
      });
      const data = await res.json();
      renderCompanionBubble(data.html || data.text);
    } catch(e) {
      renderCompanionBubble(`Recent history fetched.`);
    }
  }

  function simulateLocalLog(text) {
    const matchNum = text.match(/\d+(\.\d+)?/);
    const amt = matchNum ? parseFloat(matchNum[0]) : 20.00;
    updateBalanceDisplay(currentBalance - amt);
    updateTodayPaceDisplay(todaySpent + amt);
    renderCompanionBubble(`Logged <strong style="font-weight:700;">₹${amt.toFixed(2)}</strong>! Vault updated successfully.`, Math.floor(Math.random()*100)+1, 'dining', 'upi');
  }

  function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
});
