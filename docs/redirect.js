// 手機（觸控、窄螢幕）自動轉到手機版；在手機版選過「電腦版」或網址帶 ?desktop 就不轉
(function () {
  try {
    if (/[?&]desktop\b/.test(location.search)) { localStorage.setItem('preferDesktop', '1'); return; }
    if (localStorage.getItem('preferDesktop')) return;
  } catch (e) {}
  if (window.matchMedia && matchMedia('(max-width: 700px) and (pointer: coarse)').matches) location.replace('m/' + location.hash);
})();
