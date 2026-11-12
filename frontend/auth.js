// Fogless auth gate. Loaded in <head> of index.html BEFORE app.js.
//  - hides the page until /api/session confirms a login (no flash of the app)
//  - holds every /api/* call until that check finishes, so app.js needs no changes
//  - sends you to /login.html if you're not signed in, or if the session expires mid-use
//  - adds a LOGOUT button to the top bar and a DELETE ACCOUNT button to the tools panel
// The real protection is server-side (every /api route checks your session and scopes data to you);
// this script only decides what the browser shows.
(function () {
  'use strict';

  var LOGIN_URL = '/login.html';
  var OPEN_PATHS = ['/api/login', '/api/logout', '/api/session', '/api/signup', '/api/forgot',
                    '/api/reset-password', '/api/config'];
  var realFetch = window.fetch.bind(window);
  var NEVER = function () { return new Promise(function () {}); }; // page is navigating away

  // Hide everything until we know who is looking.
  document.documentElement.classList.add('auth-pending');
  var hide = document.createElement('style');
  hide.textContent = 'html.auth-pending body{visibility:hidden}';
  document.head.appendChild(hide);

  var leaving = false;
  function goLogin() {
    if (leaving) return; // several in-flight calls can notice at once; navigate only once
    leaving = true;
    location.replace(LOGIN_URL);
  }

  function apiPath(input) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.indexOf(location.origin) === 0) url = url.slice(location.origin.length);
    return url.indexOf('/api/') === 0 ? url : null;
  }

  var session = realFetch('/api/session', { credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (d) { return d && d.authenticated ? d : null; })
    .catch(function () { return null; });

  window.fetch = function (input, init) {
    var path = apiPath(input);
    var open = path && OPEN_PATHS.some(function (p) { return path.indexOf(p) === 0; });
    if (!path || open) return realFetch(input, init);

    return session.then(function (user) {
      if (!user) { goLogin(); return NEVER(); }
      return realFetch(input, init).then(function (res) {
        if (res.status === 401) { goLogin(); return NEVER(); }
        return res;
      });
    });
  };

  function addLogout(user) {
    var bar = document.querySelector('.chat-topbar');
    if (!bar || document.getElementById('logout-btn')) return;

    var clock = document.getElementById('greeting-time');
    var right = document.createElement('div');
    right.style.cssText = 'display:flex;align-items:center;gap:12px;';
    if (clock && clock.parentNode === bar) {
      bar.insertBefore(right, clock);
      right.appendChild(clock);
    } else {
      bar.appendChild(right);
    }

    var btn = document.createElement('button');
    btn.id = 'logout-btn';
    btn.type = 'button';
    btn.className = 'sound-btn';
    btn.style.color = 'var(--ink-muted)';
    btn.textContent = '\u238b LOGOUT';
    btn.title = 'Signed in as ' + (user && (user.email || user.username) ? (user.email || user.username) : 'you');
    btn.addEventListener('click', function () {
      btn.disabled = true;
      realFetch('/api/logout', { method: 'POST', credentials: 'same-origin' })
        .catch(function () {})
        .then(goLogin);
    });
    right.appendChild(btn);
  }

  // Permanently deletes the account and all of its data (asks for the password again).
  function deleteAccount() {
    if (!window.confirm('Delete your account and ALL of your data permanently? This cannot be undone.')) return;
    var password = window.prompt('Type your password to confirm:');
    if (!password) return;
    realFetch('/api/account/delete', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: password })
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (d) { return { res: res, d: d }; });
    }).then(function (r) {
      if (r.res.ok && r.d && r.d.ok) { window.alert('Your account has been deleted.'); goLogin(); return; }
      window.alert((r.d && r.d.text) || 'Could not delete the account (HTTP ' + r.res.status + ').');
    }).catch(function () { window.alert('Could not reach the server. Nothing was deleted.'); });
  }

  function addDeleteAccount() {
    if (document.getElementById('delete-account-btn')) return;
    var anchor = document.querySelector('.panel-btn');
    if (!anchor || !anchor.parentNode) return; // layout changed: skip quietly, nothing else depends on it
    var btn = document.createElement('button');
    btn.id = 'delete-account-btn';
    btn.type = 'button';
    btn.className = 'panel-btn';
    btn.innerHTML = '<span class="panel-btn-icon">\uD83D\uDDD1\uFE0F</span> Delete Account';
    btn.addEventListener('click', deleteAccount);
    anchor.parentNode.appendChild(btn);
  }

  session.then(function (user) {
    if (!user) { goLogin(); return; }
    document.documentElement.classList.remove('auth-pending');
    function ready() { addLogout(user); addDeleteAccount(); }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', ready);
    } else {
      ready();
    }
  });
})();
