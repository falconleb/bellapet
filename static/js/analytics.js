/**
 * PetStore Analytics — خفيف، بدون dependencies
 * يشتغل تلقائياً على كل صفحة
 */
(function () {
  'use strict';

  const VISIT_TIMEOUT_MS = 30 * 60 * 1000; // 30 دقيقة خمول = زيارة جديدة

  // ── User ID — ثابت للأبد بنفس المتصفح ──────────────────────
  function getUser() {
    let uid = localStorage.getItem('_ps_uid');
    if (!uid) {
      uid = 'u_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      localStorage.setItem('_ps_uid', uid);
    }
    return uid;
  }

  // ── Visit ID — يتجدد بعد 30 دقيقة خمول ────────────────────
  function getVisit() {
    const last = parseInt(localStorage.getItem('_ps_last') || '0', 10);
    const now  = Date.now();
    let vid    = localStorage.getItem('_ps_vid');

    if (!vid || (now - last) > VISIT_TIMEOUT_MS) {
      vid = 'v_' + now.toString(36) + Math.random().toString(36).slice(2, 8);
      localStorage.setItem('_ps_vid', vid);
    }
    localStorage.setItem('_ps_last', now);
    return vid;
  }

  // ── Device Detection ────────────────────────────────────────
  function getDeviceInfo() {
    const ua = navigator.userAgent;
    let device = 'desktop';
    if (/Mobi|Android|iPhone|iPod/i.test(ua)) device = 'mobile';
    else if (/Tablet|iPad/i.test(ua)) device = 'tablet';

    let os = 'Other';
    if (/iPhone|iPad|iPod/.test(ua)) os = 'iOS';
    else if (/Android/.test(ua))     os = 'Android';
    else if (/Windows/.test(ua))     os = 'Windows';
    else if (/Mac/.test(ua))         os = 'Mac';
    else if (/Linux/.test(ua))       os = 'Linux';

    let browser = 'Other';
    if (/CriOS|Chrome/.test(ua) && !/Edge/.test(ua)) browser = 'Chrome';
    else if (/Safari/.test(ua) && !/Chrome/.test(ua)) browser = 'Safari';
    else if (/Firefox/.test(ua))  browser = 'Firefox';
    else if (/Edge/.test(ua))     browser = 'Edge';
    else if (/SamsungBrowser/.test(ua)) browser = 'Samsung';

    return { device_type: device, os, browser,
             screen: screen.width + 'x' + screen.height,
             language: navigator.language || '' };
  }

  // ── Traffic Source ──────────────────────────────────────────
  function getSource() {
    const ref = document.referrer || '';
    const params = new URLSearchParams(location.search);
    let referrer = '';
    try { referrer = ref ? new URL(ref).hostname : ''; } catch(e) {}
    return {
      referrer,
      utm_source:   params.get('utm_source')   || '',
      utm_medium:   params.get('utm_medium')   || '',
      utm_campaign: params.get('utm_campaign') || '',
      landing_page: location.pathname,
    };
  }

  // ── Send Event ──────────────────────────────────────────────
  function track(eventType, data) {
    localStorage.setItem('_ps_last', Date.now()); // نحدّث وقت آخر نشاط
    const payload = {
      user_id:    getUser(),
      session_id: getVisit(),   // visit ID
      event_type: eventType,
      page:       location.pathname,
      ...data,
    };
    if (navigator.sendBeacon) {
      navigator.sendBeacon('/api/track', JSON.stringify(payload));
    } else {
      fetch('/api/track', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: true,
      }).catch(() => {});
    }
  }

  // ── Auto Page View ──────────────────────────────────────────
  function initSession() {
    const info = {
      user_id:    getUser(),
      session_id: getVisit(),
      ...getDeviceInfo(),
      ...getSource(),
    };
    fetch('/api/track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_type: 'page_view', ...info }),
    }).catch(() => {});
  }

  // ── Time on Page ────────────────────────────────────────────
  const _start = Date.now();

  function _sendExit() {
    const sec     = Math.round((Date.now() - _start) / 1000);
    const scrollPct = _maxScroll;
    track('page_exit', { extra: JSON.stringify({ seconds: sec, scroll_pct: scrollPct }) });
  }
  window.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') _sendExit();
  });
  window.addEventListener('pagehide', _sendExit);

  // ── Scroll Depth ─────────────────────────────────────────────
  let _maxScroll = 0;
  let _scrollMilestones = new Set();
  const MILESTONES = [25, 50, 75, 90];

  window.addEventListener('scroll', function () {
    const doc = document.documentElement;
    const pct = Math.round((doc.scrollTop / (doc.scrollHeight - doc.clientHeight || 1)) * 100);
    if (pct > _maxScroll) _maxScroll = pct;
    MILESTONES.forEach(function (m) {
      if (pct >= m && !_scrollMilestones.has(m)) {
        _scrollMilestones.add(m);
        track('scroll_depth', { extra: JSON.stringify({ pct: m }) });
      }
    });
  }, { passive: true });

  // ── Click Tracking ───────────────────────────────────────────
  document.addEventListener('click', function (e) {
    const el = e.target.closest(
      '.pet-card, .product-card, .product-card-grid, ' +
      '.nav-item, .btn-add-cart, .btn-wa-confirm, ' +
      '.section-head a, .btn-back-home, [data-track]'
    );
    if (!el) return;
    const label =
      el.dataset.track ||
      el.querySelector('.prod-name, .pet-card-name, span')?.textContent?.trim()?.slice(0, 40) ||
      el.className.split(' ')[0];
    track('click', { extra: JSON.stringify({
      el:   label,
      href: el.href ? new URL(el.href, location.href).pathname : null,
      page: location.pathname
    })});
  }, { passive: true });

  // ── Cart Abandon Detection ──────────────────────────────────
  if (location.pathname === '/cart') {
    window.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') {
        const hasItems = document.querySelectorAll('.cart-item, .ci-row').length > 0;
        if (hasItems) track('cart_abandon', {});
      }
    });
  }

  // ── Expose Global API ───────────────────────────────────────
  window.psTrack = track;

  // ── Init ────────────────────────────────────────────────────
  initSession();

})();
