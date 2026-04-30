(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.VibePreviewCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const HTML_EXTS = ['.html', '.htm'];
  const CSS_EXTS = ['.css'];
  const JS_EXTS = ['.js', '.mjs'];

  function hasExt(path, exts) {
    const p = String(path || '').toLowerCase();
    return exts.some(ext => p.endsWith(ext));
  }

  function previewSandboxBootstrapScript() {
    return `<script>
(function(){
  function makeStorage(){
    var data = Object.create(null);
    return {
      getItem: function(k){ k = String(k); return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
      setItem: function(k, v){ data[String(k)] = String(v); },
      removeItem: function(k){ delete data[String(k)]; },
      clear: function(){ data = Object.create(null); },
      key: function(i){ return Object.keys(data)[Number(i)] || null; },
      get length(){ return Object.keys(data).length; }
    };
  }
  try { Object.defineProperty(window, 'localStorage', { value: makeStorage(), configurable: true }); } catch (e) {}
  try { Object.defineProperty(window, 'sessionStorage', { value: makeStorage(), configurable: true }); } catch (e) {}
})();
<\/script>`;
  }

  function injectPreviewSandboxBootstrap(html) {
    const boot = previewSandboxBootstrapScript();
    const doc = String(html || '');
    if (/<head\b[^>]*>/i.test(doc)) {
      return doc.replace(/<head\b([^>]*)>/i, `<head$1>\n${boot}`);
    }
    if (/<html\b[^>]*>/i.test(doc)) {
      return doc.replace(/<html\b([^>]*)>/i, `<html$1>\n<head>${boot}</head>`);
    }
    return `${boot}\n${doc}`;
  }

  function normalizeAssetRef(ref) {
    return String(ref || '').split('#')[0].split('?')[0].replace(/^\.?\//, '').replace(/^\/+/, '');
  }

  function pickHtmlEntryPath(files, preferredPath) {
    const map = files || {};
    if (preferredPath && map[preferredPath] && hasExt(preferredPath, HTML_EXTS)) return preferredPath;
    const names = Object.keys(map);
    for (const pref of ['index.html', 'index.htm']) {
      const hit = names.find(name => name.toLowerCase() === pref || name.toLowerCase().endsWith('/' + pref));
      if (hit) return hit;
    }
    return names.find(name => hasExt(name, HTML_EXTS)) || '';
  }

  function findPreviewAsset(files, ref, entry) {
    const wanted = normalizeAssetRef(ref);
    if (!wanted) return '';
    const entryDir = entry && entry.includes('/') ? entry.split('/').slice(0, -1).join('/') : '';
    const candidates = [
      wanted,
      entryDir ? `${entryDir}/${wanted}` : wanted,
      wanted.split('/').pop(),
    ].filter(Boolean);
    return Object.keys(files || {}).find(key => {
      const normalized = normalizeAssetRef(key);
      const keyBase = normalized.split('/').pop();
      return candidates.some(c => normalized === c || normalized.endsWith('/' + c) || c.endsWith('/' + normalized) || keyBase === c);
    }) || '';
  }

  function scriptOpenTagWithoutSrc(match) {
    return String(match || '')
      .replace(/\s+src=(["'])[^"']*\1/i, '')
      .replace(/><\/script>$/i, '>');
  }

  function buildCombinedHtmlSrcdoc(files, preferredEntry) {
    const map = files || {};
    const entry = pickHtmlEntryPath(map, preferredEntry);
    if (!entry) return null;

    let html = String(map[entry] || '');
    const used = new Set([entry]);

    html = html.replace(
      /<link\b[^>]*?rel=["']stylesheet["'][^>]*?href=["']([^"']+)["'][^>]*?>/gi,
      (match, href) => {
        const key = findPreviewAsset(map, href, entry);
        if (key && hasExt(key, CSS_EXTS)) {
          used.add(key);
          return `<style>\n${map[key]}\n</style>`;
        }
        return match;
      }
    );

    html = html.replace(
      /<script\b[^>]*?src=["']([^"']+)["'][^>]*?><\/script>/gi,
      (match, src) => {
        const key = findPreviewAsset(map, src, entry);
        if (key && hasExt(key, JS_EXTS)) {
          used.add(key);
          return `${scriptOpenTagWithoutSrc(match)}\n${String(map[key] || '').replace(/<\/script/gi, '<\\/script')}\n<\/script>`;
        }
        return match;
      }
    );

    const orphanStyle = Object.keys(map)
      .filter(key => !used.has(key) && hasExt(key, CSS_EXTS))
      .map(key => `<style>\n${map[key]}\n</style>`)
      .join('\n');
    const orphanScript = Object.keys(map)
      .filter(key => !used.has(key) && hasExt(key, JS_EXTS))
      .map(key => `<script>\n${String(map[key] || '').replace(/<\/script/gi, '<\\/script')}\n<\/script>`)
      .join('\n');
    const tail = [orphanStyle, orphanScript].filter(Boolean).join('\n');

    if (tail) {
      if (/<\/body>/i.test(html)) html = html.replace(/<\/body>/i, `${tail}\n</body>`);
      else html += `\n${tail}`;
    }

    return injectPreviewSandboxBootstrap(html);
  }

  return {
    buildCombinedHtmlSrcdoc,
    findPreviewAsset,
    hasExt,
    injectPreviewSandboxBootstrap,
    normalizeAssetRef,
    pickHtmlEntryPath,
    previewSandboxBootstrapScript,
  };
});
