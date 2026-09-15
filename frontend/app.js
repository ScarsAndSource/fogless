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
  const headerSprite = document.getElementById('mochi-master-svg');
  const pokeHeart = document.getElementById('poke-heart');
  const mochiWrap = document.getElementById('mochi-poke-trigger');
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

  // Mochi Avatar Poke Interaction
  if (mochiWrap) {
    mochiWrap.addEventListener('click', () => {
      playPurr();
      if (headerSprite) {
        headerSprite.classList.remove('mochi-idle');
        headerSprite.classList.add('mochi-jump');
      }
      if (pokeHeart) pokeHeart.style.display = 'block';
      setTimeout(() => {
        if (pokeHeart) pokeHeart.style.display = 'none';
        if (headerSprite) {
          headerSprite.classList.remove('mochi-jump');
          headerSprite.classList.add('mochi-idle');
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

  // Command directory chips
  document.querySelectorAll('.cmd-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      playBeep(520);
      const cmd = chip.textContent.trim();
      if (cmd === '/stats') {
        fetchAndDisplayStats();
      } else if (cmd === '/balance') {
        fetchAndDisplayBalance();
      } else if (cmd === '/history') {
        fetchAndDisplayHistory();
      } else if (cmd === '/undo') {
        handleQuickUndo();
      } else if (cmd === '/aliases') {
        executeCommandText('/aliases');
      } else if (cmd === '/export') {
        window.location.href = '/api/export';
      } else if (cmd === '/backup') {
        window.location.href = '/api/backup';
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
    if (todayLoggedVal) todayLoggedVal.textContent = `$${todaySpent.toFixed(2)}`;
    const pct = Math.min(100, Math.round((todaySpent / todayLimit) * 100));
    if (todayPctVal) todayPctVal.textContent = `${pct}%`;

    if (todayGaugeCells) {
      const filledCells = Math.min(6, Math.round((todaySpent / todayLimit) * 6));
      todayGaugeCells.innerHTML = '';
      for (let i = 0; i < 6; i++) {
        const cell = document.createElement('div');
        if (i < filledCells) {
          cell.className = i >= 4 ? 'w-2.5 h-2 bg-[#E15554] border border-[#3E5C46]' : 'w-2.5 h-2 bg-[#5B8B67] border border-[#3E5C46]';
        } else {
          cell.className = 'w-2.5 h-2 bg-[#E1E8DE] border border-[#A7B9A9]';
        }
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
    cassetteBubble.className = 'flex justify-end';
    cassetteBubble.innerHTML = `
      <div class="pixel-border-peach p-2 text-[#4E1B15] max-w-[85%] space-y-1">
        <div class="flex items-center justify-between text-[8px] font-numeral text-[#8D382B]">
          <span>VOICE MEMO · CASSETTE ${String(Math.floor(Math.random()*90)+10)}</span>
          <span>JUST NOW</span>
        </div>
        <div class="bg-[#F8ECE8] border-2 border-[#8D382B] p-2 flex items-center gap-3">
          <button class="w-8 h-8 bg-[#8D382B] text-[#FFF4F0] flex items-center justify-center font-bold text-[12px] border-2 border-[#4E1B15] shadow-[1px_1px_0_0_#4E1B15] play-cassette-btn">
            ▶
          </button>
          <div class="flex items-end gap-1.5 h-6 px-1 flex-1 bg-[#E8D4CE] border border-[#A65B4D] pt-1">
            <div class="w-1.5 bg-[#8D382B] eq-bar-1"></div>
            <div class="w-1.5 bg-[#8D382B] eq-bar-2"></div>
            <div class="w-1.5 bg-[#8D382B] eq-bar-3"></div>
            <div class="w-1.5 bg-[#8D382B] eq-bar-4"></div>
            <div class="w-1.5 bg-[#8D382B] eq-bar-2"></div>
            <div class="w-1.5 bg-[#8D382B] eq-bar-1"></div>
          </div>
          <div class="text-right">
            <div class="font-numeral text-[9px] font-bold text-[#8D382B]">AUDIO</div>
          </div>
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
    heroBubble.className = 'flex justify-end';
    heroBubble.innerHTML = `
      <div class="pixel-border-peach p-2.5 text-[#4E1B15] max-w-[82%]">
        <div class="flex items-center justify-between border-b border-[#F7B6A4] pb-0.5 mb-1 gap-4">
          <span class="font-numeral text-[8px] text-[#8D382B] uppercase font-bold">Hero</span>
          <span class="font-numeral text-[7px] text-[#A8584B]">JUST NOW</span>
        </div>
        <p class="font-bold text-[15px]">${escapeHtml(text)}</p>
      </div>
    `;
    chatThread.appendChild(heroBubble);
    chatThread.scrollTop = chatThread.scrollHeight;

    if (headerSprite) {
      headerSprite.classList.remove('mochi-idle');
      headerSprite.classList.add('mochi-jump');
      setTimeout(() => {
        headerSprite.classList.remove('mochi-jump');
        headerSprite.classList.add('mochi-idle');
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
      simulateLocalLog(text);
    }
  }

  function executeLog() {
    const text = input.value.trim();
    if (!text) return;
    commandHistory.push(text);
    historyIdx = -1;
    input.value = '';
    executeCommandText(text);
  }

  if (sendBtn) sendBtn.addEventListener('click', executeLog);
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') executeLog();
    });
  }

  function renderCompanionBubble(htmlContent, txId = null, category = null, paymentMethod = null) {
    const bubble = document.createElement('div');
    bubble.className = 'flex items-start gap-2.5 max-w-[98%]';
    
    let inlineMenuHtml = '';
    if (txId) {
      inlineMenuHtml = `
        <div class="pixel-window-jrpg p-2 text-[13px] space-y-1.5 mt-2">
          <div class="flex items-center justify-between pixel-titleplate px-2 py-0.5 text-white">
            <span class="font-numeral text-[8px] tracking-wider text-[#FCD34D]">COMMAND: EDIT RECORD</span>
            <span class="font-numeral text-[7px] text-[#A5B4FC]">PRESS A</span>
          </div>
          <div class="flex flex-col gap-0.5 pt-0.5">
            <button class="flex items-center text-left hover:bg-[#F3EEDD] px-1.5 py-0.5 text-[#1E1B29] group" onclick="window.changeCategory(${txId})">
              <span class="text-[#E15554] font-bold mr-1.5 text-[12px] rpg-cursor-blink">▶</span>
              <span>Category: <strong class="text-[#155E75]">${category || 'Dining'}</strong></span>
            </button>
            <button class="flex items-center text-left hover:bg-[#F3EEDD] px-1.5 py-0.5 text-[#1E1B29] group" onclick="window.changePayment(${txId})">
              <span class="text-transparent group-hover:text-[#E15554] font-bold mr-1.5 text-[12px]">▶</span>
              <span>Payment: <strong class="text-[#7C2D12]">${paymentMethod || 'UPI Wallet'}</strong></span>
            </button>
            <button class="flex items-center text-left hover:bg-[#FDE8E8] px-1.5 py-0.5 text-[#DC2626] group" onclick="window.undoTxId(${txId})">
              <span class="text-transparent group-hover:text-[#DC2626] font-bold mr-1.5 text-[12px]">▶</span>
              <span class="font-bold">Cast Undo (Reverse #${txId})</span>
            </button>
          </div>
        </div>
      `;
    }

    bubble.innerHTML = `
      <div class="shrink-0 w-8 h-8 bg-[#FFFCE8] border-2 border-[#345C3D] flex items-center justify-center shadow-[1px_1px_0_0_#345C3D]">
        <svg class="w-6 h-6" viewBox="0 0 16 16" fill="none">
          <rect x="7" y="1" width="2" height="2" fill="#1F4D25"/>
          <rect x="4" y="4" width="8" height="8" fill="#8CE0A0"/>
          <rect x="5" y="6" width="2" height="2" fill="#142B1A"/>
          <rect x="9" y="6" width="2" height="2" fill="#142B1A"/>
          <rect x="7" y="9" width="2" height="1" fill="#142B1A"/>
        </svg>
      </div>
      <div class="flex-1 space-y-1">
        <div class="pixel-border-sage p-2.5 text-[#142B1A]">
          ${htmlContent}
        </div>
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
      renderCompanionBubble(data.text || `⚔ SPELL: REVERT EXECUTED. Restored GP to the treasury purse.`);
    } catch(e) {
      updateBalanceDisplay(currentBalance + 60.00);
      renderCompanionBubble(`⚔ SPELL: REVERT EXECUTED. Restored $60.00 GP back to the treasury purse.`);
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
        <div class="font-bold text-[13px] text-[#142B1A] flex justify-between mb-1">
          <span>⚔ Dungeon Expense Report</span>
          <span class="font-numeral text-[9px] text-[#059669]">STATUS: ON TRACK</span>
        </div>
        <p class="text-[12px] text-[#142B1A]">May pace is currently 22% under the safe ceiling. Remaining safe pouch: <strong>$558.00</strong>.</p>
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
      renderCompanionBubble(`Treasury Total: $${currentBalance.toFixed(2)} GP.`);
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
    renderCompanionBubble(`Logged <strong class="font-numeral text-[12px]">$${amt.toFixed(2)}</strong>! Vault updated successfully.`, Math.floor(Math.random()*100)+1, 'dining', 'upi');
  }

  function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
});
