// Exercise Chrome's native editing events without sending keys to a live guest.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({channel:'chrome'});
  try {
    const page = await browser.newPage();
    const sent = [];
    await page.routeWebSocket('**/display', () => {});
    await page.routeWebSocket('**/input', ws => {
      ws.onMessage(raw => {
        const msg = JSON.parse(raw);
        sent.push(msg);
        if (msg.id) ws.send(JSON.stringify({id:msg.id,result:''}));
      });
      ws.send(JSON.stringify({ready:true}));
    });
    const base = 'http://127.0.0.1:6083';
    const inventory = await (await page.request.get(base + '/api/management')).json();
    const imported = inventory.vms.find(vm => vm.legacy_container);
    assert.ok(imported, 'Register the existing VM before running this smoke test');
    await page.goto(base + '/vms/' + imported.id + '/console/');
    await page.locator('#surface').focus();
    assert.equal(await page.evaluate(()=>document.activeElement.id), 'ime');
    const cdp = await page.context().newCDPSession(page);
    const mark = text => cdp.send('Input.imeSetComposition', {
      text, selectionStart:text.length, selectionEnd:text.length
    });
    await mark('にほん'); await mark('日本');
    assert.equal(await page.locator('#ime').evaluate(el=>el.classList.contains('preedit')),true);
    await cdp.send('Input.insertText',{text:'日本'});
    await mark('中止'); await mark('');
    await cdp.send('Input.insertText',{text:'🍵'});
    await page.keyboard.type('a');
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>document.querySelector('#ime').value==='');
    await page.waitForTimeout(100);
    assert.deepEqual(sent.filter(m=>m.op==='type').map(m=>m.text),['日本','🍵']);
    assert.deepEqual(sent.filter(m=>m.op==='key').map(m=>[m.key,m.down]),
      [['a',true],['a',false],['ret',true],['ret',false]]);
    assert.equal(sent.some(m=>m.op==='paste'),false);
    await page.locator('#compose').click();
    assert.equal(await page.locator('#draft').evaluate(el=>el===document.activeElement),true);
    console.log('inline composition, cancel, single commit, physical keys and dialog passed');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
