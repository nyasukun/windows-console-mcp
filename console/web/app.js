import './walkthrough.js';
import {SpiceMainConn} from '/spice/main.js';

const $ = id => document.getElementById(id);
const surface = $('surface'), frame = $('frame'), stage = $('stage'), display = $('display');
const ime = $('ime');
const heldKeys = new Set();
let pendingPrintable;
const sessionMenu = $('session-menu');
const trigger = $('menu-trigger');
const iconPaths = {
  monitor: 'M3 4h18v13H3z M8 21h8 M12 17v4',
  type: 'M3 6h12 M9 6v13 M6 19h6 M15 13h6 M18 10v9',
  paste: 'M8 5H5v16h14V5h-3 M9 3h6v4H9z',
  copy: 'M8 8h12v13H8z M16 5V3H4v14h1',
  expand: 'M3 9V3h6 M15 3h6v6 M21 15v6h-6 M9 21H3v-6',
  settings: 'M3 7h4 M11 7h10 M3 17h10 M17 17h4 M7 4v6h4V4z M13 14v6h4v-6z',
  fit: 'M3 9V4h5 M16 4h5v5 M21 15v5h-5 M8 20H3v-5 M7 8h10v8H7z',
  pointer: 'M4 3l15 10-7 1-3 7z',
  close: 'M6 6l12 12 M18 6L6 18',
  arrow: 'M4 12h15 M14 6l6 6-6 6'
};
for (const el of document.querySelectorAll('[data-icon]')) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24');
  svg.setAttribute('aria-hidden','true');
  const line = document.createElementNS(svg.namespaceURI,'path');
  for (const [name,value] of Object.entries({d:iconPaths[el.dataset.icon],fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round'})) {
    line.setAttribute(name,value);
  }
  svg.append(line);
  el.append(svg);
}
new ResizeObserver(() => {
  sessionMenu.style.setProperty('--menu-top', (trigger.closest('header').getBoundingClientRect().bottom+5)+'px');
}).observe(trigger.closest('header'));
function returnToConsole() {
  sessionMenu.hidePopover();
  surface.focus({preventScroll:true});
}
document.addEventListener('keydown', e => {
  if (e.ctrlKey && e.altKey && e.code==='Home' && !$('editor').open) {
    e.preventDefault();
    e.stopPropagation();
    release();
    if (sessionMenu.matches(':popover-open')) returnToConsole();
    else { sessionMenu.showPopover(); $('fit').focus(); }
  }
},true);
const socketURL = path => (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
const input = new WebSocket(socketURL('/input'));
let screen, canvas, ready = false, fit = true, received = 0, ack = 0, animation = 0;
let position = {x:16384,y:16384}, lastSent = '', noticeTimer, composing = false;
const pending = new Map();
function notify(text) {
  $('notice').textContent = text;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => $('notice').textContent = '', 6000);
}
function status() {
  const connected = ready && !!canvas;
  $('status').textContent = connected ? '接続済み' : '接続待ち';
  $('status').classList.toggle('online', connected);
}
function failed(text) {
  $('status').textContent = text;
  $('status').classList.remove('online');
  $('reconnect').hidden = false;
}
function send(msg) {
  if (!ready || input.readyState !== WebSocket.OPEN) return false;
  input.send(JSON.stringify(msg));
  return true;
}
function release() {
  heldKeys.clear();
  pendingPrintable = null;
  cancelAnimationFrame(animation);
  animation = 0;
  lastSent = '';
  send({op:'release'});
}
function request(op, text) {
  return new Promise((resolve,reject) => {
    const id = crypto.randomUUID();
    if (!send({op,text,id})) return reject(Error('未接続です'));
    pending.set(id,{resolve,reject});
  });
}
input.onmessage = event => {
  const msg = JSON.parse(event.data);
  if (msg.ready) {
    ready = true;
    screen = new SpiceMainConn({uri:socketURL('/display'), screen_id:'display',
      onerror:() => { release(); input.close(); failed('映像切断'); }});
    status();
  }
  if (msg.motion !== undefined) ack = msg.motion;
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id); pending.delete(msg.id);
    if (msg.error) p.reject(Error(msg.error)); else p.resolve(msg.result);
  } else if (msg.error) notify(msg.error);
};
input.onclose = () => {
  ready=false; screen?.stop();
  for (const p of pending.values()) p.reject(Error('切断されました'));
  pending.clear(); failed('切断・ほかのタブが使用中');
};
$('reconnect').onclick = () => location.reload();
function layout() {
  canvas = display.querySelector('#spice_surface_0');
  if (!canvas) return;
  const scale = fit ? Math.min(stage.clientWidth/canvas.width,stage.clientHeight/canvas.height) : 1;
  display.style.transform = 'scale('+scale+')';
  frame.style.width = canvas.width*scale+'px';
  frame.style.height = canvas.height*scale+'px';
  status();
}
new MutationObserver(layout).observe(display,{childList:true,subtree:true,attributes:true,attributeFilter:['width','height']});
new ResizeObserver(layout).observe(stage);
$('fit').onclick = () => { fit=!fit; $('fit').setAttribute('aria-pressed',fit); layout(); };
$('full').onclick = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen()).catch(e=>notify(e.message));
function coords(e) {
  const r = surface.getBoundingClientRect();
  const clamp = v => Math.min(32767,Math.max(0,Math.round(v)));
  if (document.pointerLockElement === surface) {
    return {x:clamp(position.x + e.movementX*32767/r.width),y:clamp(position.y + e.movementY*32767/r.height)};
  }
  return {x:clamp((e.clientX-r.left)*32767/r.width),y:clamp((e.clientY-r.top)*32767/r.height)};
}
function flush() {
  animation=0;
  const stamp=position.x+','+position.y;
  if (lastSent!==stamp && send({op:'move',...position,seq:received})) lastSent=stamp;
}
function motion(e) {
  position=coords(e); received++;
  $('marker').style.left=position.x/327.67+'%'; $('marker').style.top=position.y/327.67+'%';
  if (!animation) animation=requestAnimationFrame(flush);
}
surface.onpointermove=e=>{if(document.pointerLockElement!==surface) motion(e);};
surface.onmousemove=e=>{
  if (document.pointerLockElement===surface) { motion(e); return; }
  const p=coords(e);
  if (p.x!==position.x || p.y!==position.y) motion(e);
};
function button(e,down) {
  if (e.button<0 || e.button>4) return;
  e.preventDefault();
  if (down && composing) ime.blur();
  ime.focus({preventScroll:true});
  if (down) surface.setPointerCapture(e.pointerId);
  if (!document.pointerLockElement) position=coords(e);
  if (down && e.button===0) {
    ime.style.setProperty('--anchor-x', position.x/327.67+'%');
    ime.style.setProperty('--anchor-y', position.y/327.67+'%');
    placeIME();
  }
  send({op:'button',...position,button:e.button,down});
}
surface.onpointerdown=e=>button(e,true);
surface.onpointerup=e=>button(e,false);
surface.onpointercancel=release;
surface.oncontextmenu=e=>e.preventDefault();
surface.addEventListener('wheel',e=>{e.preventDefault(); if(e.deltaY)send({op:'wheel',delta:e.deltaY});},{passive:false});
$('lock').onclick=async()=>{
  try {
    if (document.pointerLockElement) document.exitPointerLock();
    else {returnToConsole(); await surface.requestPointerLock();}
  } catch(e){notify(e.message);}
};
document.onpointerlockchange=()=>{
  const locked=document.pointerLockElement===surface;
  $('marker').hidden=!locked;
  $('lock').setAttribute('aria-pressed',locked);
  if (!locked) release();
};
setInterval(()=>{$('metrics').textContent='移動 '+received+'  /  QEMU '+ack;},300);

const names={Backspace:'backspace',Tab:'tab',Enter:'ret',Escape:'esc',Space:'spc',
  ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down',Home:'home',End:'end',
  PageUp:'pgup',PageDown:'pgdn',Delete:'delete',Insert:'insert',ShiftLeft:'shift',ShiftRight:'shift_r',
  ControlLeft:'ctrl',ControlRight:'ctrl_r',AltLeft:'alt',AltRight:'alt_r',CapsLock:'caps_lock',
  Minus:'minus',Equal:'equal',BracketLeft:'bracket_left',BracketRight:'bracket_right',Backslash:'backslash',
  Semicolon:'semicolon',Quote:'apostrophe',Backquote:'grave',Comma:'comma',Period:'dot',Slash:'slash',
  IntlYen:'yen',IntlRo:'ro',Convert:'henkan',NonConvert:'muhenkan',KanaMode:'katakanahiragana',
  NumpadEnter:'kp_enter',NumpadAdd:'kp_add',NumpadSubtract:'kp_subtract',NumpadMultiply:'kp_multiply',
  NumpadDivide:'kp_divide',NumpadDecimal:'kp_decimal',NumLock:'num_lock',ContextMenu:'menu'};
function qcode(code){
  if (/^Key[A-Z]$/.test(code)) return code[3].toLowerCase();
  if (/^Digit\d$/.test(code)) return code[5];
  if (/^Numpad\d$/.test(code)) return 'kp_'+code[6];
  if (/^F([1-9]|1[0-2])$/.test(code)) return code.toLowerCase();
  return names[code];
}
async function paste(text){if(text) await request('paste',text);}
async function copy(){
  const result=request('copy').then(text=>text.replaceAll('\r\n','\n'));
  // Start the clipboard write while the click/shortcut still grants user activation.
  try { await navigator.clipboard.write([new ClipboardItem({'text/plain':result.then(s=>new Blob([s],{type:'text/plain'}))})]);
  } catch (error) {
    const text=await result;
    $('compose').click(); $('draft').value=text; $('draft').select();
    notify('ブラウザで選択したテキストをコピーしてください'); return;
  }
  notify('選択したテキストをコピーしました');
}
function report(p){p.catch(e=>notify(e.message));}
surface.onkeydown=e=>{
  if (composing || e.isComposing || e.keyCode===229 || e.key==='Process' || e.key==='Dead') return;
  if (e.code==='Escape' && document.pointerLockElement===surface) {
    e.preventDefault();
    document.exitPointerLock();
    return;
  }
  pendingPrintable=null;
  // Let Chrome decide whether a printable key starts a host IME composition.
  if (!e.ctrlKey && !e.metaKey && !e.altKey && e.key.length===1) {
    pendingPrintable={code:e.code, text:e.key};
    return;
  }
  if((e.metaKey || e.ctrlKey) && !e.altKey && ['KeyC','KeyV'].includes(e.code)){
    e.preventDefault(); release();
    if (e.code === 'KeyC') { if (!e.repeat) report(copy()); }
    else if (!e.repeat) report(navigator.clipboard.readText().then(paste).catch(err=>{
      $('compose').click(); throw Error('入力欄に貼り付けて「Windows に送信」を押してください。');
    }));
    return;
  }
  const key=qcode(e.code);
  if (e.metaKey && e.repeat) { e.preventDefault(); return; }
  if (e.metaKey && key && !e.repeat) {
    e.preventDefault();
    for (const [k, down] of [['ctrl',true],[key,true],[key,false],['ctrl',false]]) send({op:'key',key:k,down});
    return;
  }
  if(key){e.preventDefault();heldKeys.add(key);send({op:'key',key,down:true});}
};
surface.onkeyup=e=>{
  pendingPrintable=null;
  const key=qcode(e.code);
  if (heldKeys.delete(key)) {e.preventDefault();send({op:'key',key,down:false});}
};
function placeIME() {
  ime.style.left='clamp(0px, var(--anchor-x, 50%), max(0px, 100% - '+ime.offsetWidth+'px))';
  ime.style.top='clamp(0px, var(--anchor-y, 50%), max(0px, 100% - '+ime.offsetHeight+'px))';
}
ime.addEventListener('compositionstart',()=>{
  composing=true;release();ime.classList.add('preedit');placeIME();
});
ime.addEventListener('compositionupdate',e=>{
  ime.style.setProperty('--ime-width',Math.max(4,Math.min(32,Array.from(e.data).length+1))+'em');
  placeIME();
});
ime.addEventListener('compositionend',e=>{
  composing=false;
  ime.classList.remove('preedit');
  if (e.data) report(request('type',e.data));
  // Chrome may finish the editable element's own update after compositionend.
  setTimeout(()=>{if(!composing) ime.value='';},0);
});
ime.onbeforeinput=e=>{
  if (composing || e.isComposing || e.inputType!=='insertText' || !e.data) return;
  e.preventDefault();
  const key=pendingPrintable && qcode(pendingPrintable.code);
  if (key && pendingPrintable.text===e.data && /^[ -~]$/.test(e.data)) {
    heldKeys.add(key);send({op:'key',key,down:true});
  } else {
    report(request('type',e.data));
  }
  pendingPrintable=null;
};
surface.onfocus=()=>ime.focus({preventScroll:true});
surface.onpaste=e=>{e.preventDefault();report(paste(e.clipboardData.getData('text/plain')));};
window.addEventListener('blur',release);ime.onblur=release;
document.addEventListener('visibilitychange',()=>{if(document.hidden)release();});
for (const button of sessionMenu.querySelectorAll('[data-shortcut]')) {
  button.onclick = () => {
    const keys = {run:['meta_l','r'],switch:['alt','tab'],secure:['ctrl','alt','delete']}[button.dataset.shortcut];
    returnToConsole();
    release();
    for (const key of keys) send({op:'key',key,down:true});
    for (const key of keys.reverse()) send({op:'key',key,down:false});
  };
}
$('win').onclick=()=>{returnToConsole();send({op:'key',key:'meta_l',down:true});send({op:'key',key:'meta_l',down:false});};
$('paste').onclick=()=>{surface.focus();report(navigator.clipboard.readText().then(paste));};
$('copy').onclick=()=>{surface.focus();report(copy());};
$('compose').onclick=()=>{$('editor').showModal();$('draft').focus();};
$('editor').onclose=()=>surface.focus();
$('send').onclick=async()=>{
  $('send').disabled=true;
  try {await paste($('draft').value);$('draft').value='';$('editor').close();}
  catch(e){notify(e.message);}finally{$('send').disabled=false;}
};
$('draft').onkeydown=e=>{if(!e.isComposing && e.key==='Enter' && (e.ctrlKey||e.metaKey)){e.preventDefault();$('send').click();}};
