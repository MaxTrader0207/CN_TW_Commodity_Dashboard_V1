// 先套用主題，避免載入時閃一下（儲存失敗時就跟隨系統）
try { const t = localStorage.getItem('theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}
