const assert = require('assert');
const core = require('../assets/preview_core.js');

function testInlineAssetsAndBootstrap() {
  const srcdoc = core.buildCombinedHtmlSrcdoc({
    'index.html': '<!DOCTYPE html><html><head><link rel="stylesheet" href="./style.css"></head><body><div id="app"></div><script src="./script.js"></script></body></html>',
    'style.css': 'body { color: red; }',
    'script.js': 'document.body.dataset.ready = "1";',
  }, 'index.html');

  assert(srcdoc.includes('Object.defineProperty(window, \'localStorage\''), 'storage shim is injected');
  assert(srcdoc.includes('<style>\nbody { color: red; }\n</style>'), 'stylesheet is inlined');
  assert(srcdoc.includes('document.body.dataset.ready = "1";'), 'script is inlined');
  assert(!srcdoc.includes('src="./script.js"'), 'external script reference is removed');
}

function testNestedEntryRelativeAssets() {
  const srcdoc = core.buildCombinedHtmlSrcdoc({
    'demo/index.html': '<html><head><link rel="stylesheet" href="style.css"></head><body><script src="app.js"></script></body></html>',
    'demo/style.css': '.hero { min-height: 100vh; }',
    'demo/app.js': 'window.__previewOk = true;',
  }, 'demo/index.html');

  assert(srcdoc.includes('.hero { min-height: 100vh; }'), 'relative nested stylesheet is inlined');
  assert(srcdoc.includes('window.__previewOk = true;'), 'relative nested script is inlined');
}

function testMissingHtmlReturnsNull() {
  const srcdoc = core.buildCombinedHtmlSrcdoc({ 'style.css': 'body{}' }, '');
  assert.strictEqual(srcdoc, null);
}

testInlineAssetsAndBootstrap();
testNestedEntryRelativeAssets();
testMissingHtmlReturnsNull();
console.log('preview_core tests passed');
