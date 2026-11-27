import {api, socket} from './endpoint.js';

const tabs = document.querySelector('#tabs');
const status = document.querySelector('#status');
const address = document.querySelector('#address');
const frame = document.querySelector('#page-frame');
const input = document.querySelector('#page-input');
let active, connection, frameURL, tabSignature, moving, moveFrame;

function note(message) {
  status.textContent = message;
}

function send(event) {
  if (connection?.readyState === WebSocket.OPEN && connection.bufferedAmount < 32768) {
    connection.send(JSON.stringify(event));
  }
}

function selectTab(id) {
  if (id === active && connection?.readyState <= WebSocket.OPEN) return;
  active = id;
  if (moveFrame) cancelAnimationFrame(moveFrame);
  moveFrame = 0;
  connection?.close();
  connection = undefined;
  frame.hidden = true;
  frame.removeAttribute('src');
  if (frameURL) URL.revokeObjectURL(frameURL);
  frameURL = undefined;
  if (!id) {
    note('タブなし');
    return;
  }
  note('接続中');
  const ws = connection = socket('/browser/display/' + id);
  ws.onmessage = event => {
    if (ws !== connection) return;
    if (typeof event.data === 'string') {
      note(JSON.parse(event.data).error);
      return;
    }
    const previous = frameURL;
    frameURL = URL.createObjectURL(event.data);
    frame.src = frameURL;
    frame.hidden = false;
    if (previous) URL.revokeObjectURL(previous);
    note('接続済み');
  };
  ws.onclose = () => { if (ws === connection) note('切断されました。タブを選び直してください。'); };
}

function tabButton(item) {
  const pair = document.createElement('div');
  pair.className = 'tab-pair' + (item.id === active ? ' active' : '');
  const open = document.createElement('button');
  const title = item.title || item.url || '新しいタブ';
  open.type = 'button';
  open.textContent = open.title = title;
  open.setAttribute('role', 'tab');
  open.setAttribute('aria-selected', String(item.id === active));
  open.onclick = () => {
    address.value = item.url;
    selectTab(item.id);
    refreshTabs();
  };
  const close = document.createElement('button');
  close.type = 'button';
  close.textContent = '×';
  close.setAttribute('aria-label', title + ' を閉じる');
  close.onclick = () => api('/browser/tabs/' + item.id, 'DELETE').then(refreshTabs)
    .catch(error => note(error.message));
  pair.append(open, close);
  return pair;
}

async function refreshTabs() {
  try {
    const items = await api('/browser/tabs');
    let current = items.find(item => item.id === active);
    if (!current) {
      current = items[0];
      selectTab(current?.id);
    }
    if (document.activeElement !== address) address.value = current?.url || '';
    const signature = JSON.stringify([active, items]);
    if (signature !== tabSignature) {
      tabs.replaceChildren(...items.map(tabButton));
      tabSignature = signature;
    }
  } catch (error) {
    note(error.message);
  }
}

document.querySelector('#new-tab').onclick = async () => {
  try {
    const item = await api('/browser/tabs', 'POST');
    selectTab(item.id);
    address.value = '';
    address.focus();
    await refreshTabs();
  } catch (error) { note(error.message); }
};

document.querySelector('#address-form').onsubmit = async event => {
  event.preventDefault();
  const entered = address.value.trim();
  if (!entered) return;
  const url = entered === 'about:blank' || entered.includes('://') ? entered : 'http://' + entered;
  try {
    if (active) await api('/browser/tabs/' + active, 'PUT', {url});
    else selectTab((await api('/browser/tabs', 'POST', {url})).id);
    input.focus({preventScroll: true});
    await refreshTabs();
  } catch (error) { note(error.message); }
};

const modifiers = event => Number(event.altKey) + 2 * event.ctrlKey + 4 * event.metaKey + 8 * event.shiftKey;
function pointer(event, type) {
  const rect = frame.getBoundingClientRect();
  if (!rect.width || !frame.naturalWidth) return;
  return {type, x: (event.clientX - rect.left) * frame.naturalWidth / rect.width,
    y: (event.clientY - rect.top) * frame.naturalHeight / rect.height,
    button: ['left', 'middle', 'right'][event.button] || 'none', buttons: event.buttons & 7,
    modifiers: modifiers(event), clickCount: Math.min(3, event.detail || 1)};
}

frame.onpointermove = event => {
  moving = pointer(event, 'mouseMoved');
  if (!moveFrame) moveFrame = requestAnimationFrame(() => {
    moveFrame = 0;
    if (moving) send(moving);
  });
};
frame.onpointerdown = event => {
  event.preventDefault();
  frame.setPointerCapture(event.pointerId);
  input.focus({preventScroll: true});
  const data = pointer(event, 'mousePressed');
  if (data) send(data);
};
frame.onpointerup = event => {
  const data = pointer(event, 'mouseReleased');
  if (data) send(data);
};
frame.oncontextmenu = event => event.preventDefault();
frame.onwheel = event => {
  event.preventDefault();
  const data = pointer(event, 'mouseWheel');
  if (data) {
    const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? frame.clientHeight : 1;
    send({...data, deltaX: event.deltaX * scale, deltaY: event.deltaY * scale});
  }
};

const physical = event => event.key.length !== 1 || event.ctrlKey || event.altKey || event.metaKey;
function key(event, type) {
  if (event.isComposing || !physical(event) || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v')) return;
  event.preventDefault();
  send({type, key: event.key, code: event.code, keyCode: event.keyCode, modifiers: modifiers(event)});
}
input.onkeydown = event => key(event, 'rawKeyDown');
input.onkeyup = event => key(event, 'keyUp');
input.onbeforeinput = event => {
  if (!event.isComposing && event.data && event.inputType === 'insertText') {
    event.preventDefault();
    send({type: 'text', text: event.data});
  }
};
input.oncompositionend = event => {
  if (event.data) send({type: 'text', text: event.data});
  input.value = '';
};
input.onpaste = event => {
  event.preventDefault();
  const text = event.clipboardData.getData('text/plain');
  for (let index = 0; index < text.length; index += 4000) send({type: 'text', text: text.slice(index, index + 4000)});
};

async function pollTabs() {
  await refreshTabs();
  setTimeout(pollTabs, 1800);
}
pollTabs();
