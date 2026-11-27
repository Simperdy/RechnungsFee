/* Anti-Flash-Theme-Initialisierung.
   Muss inline im <head> laufen (vor React), damit Hell/Dunkel schon beim ersten
   Paint stimmt. Liegt bewusst als externes File in public/ statt inline, weil die
   Server-Modus-CSP (default-src 'self') Inline-Scripts blockiert.
   Logik identisch zu applyTheme() in src/hooks/useAnsicht.ts. */
(function () {
  try {
    var s = localStorage.getItem('ansicht_einstellungen');
    var p = s ? JSON.parse(s) : {};
    var fs = p.farbschema || 'system';
    var dark = fs === 'dunkel' || (fs === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  } catch (e) { /* ohne localStorage hell erzwingen ist besser als crashen */ }
})();
