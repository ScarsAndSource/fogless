// Fogless auth gate. Loaded in <head> of index.html BEFORE app.js.
//  - hides the page until /api/session confirms a login (no flash of the app)
//  - holds every /api/* call until that check finishes, so app.js needs no changes
//  - sends you to /login.html if you're not signed in, or if the session expires mid-use
//  - adds a LOGOUT button to the top bar
(function () {
  'use strict';

  var LOGIN_URL = '/login.html';
  var OPEN_PATHS = ['/api/login', '/api/logout', '/api/session'];
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
    btn.title = 'Signed in as ' + (user && user.username ? user.username : 'you');
    btn.addEventListener('click', function () {
      btn.disabled = true;
      realFetch('/api/logout', { method: 'POST', credentials: 'same-origin' })
        .catch(function () {})
        .then(goLogin);
    });
    right.appendChild(btn);
  }

  session.then(function (user) {
    if (!user) { goLogin(); return; }
    document.documentElement.classList.remove('auth-pending');
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { addLogout(user); });
    } else {
      addLogout(user);
    }
  });
})();
