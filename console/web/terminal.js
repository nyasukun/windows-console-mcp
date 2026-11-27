import {socket} from './endpoint.js';

const element = document.querySelector('#terminal');
const status = document.querySelector('#status');
const reconnect = document.querySelector('#reconnect');
const terminal = new Terminal({cursorBlink: true, fontSize: 15, scrollback: 5000,
  theme: {background: '#0c111a', foreground: '#e6edf6', cursor: '#a9caff'}});
const fit = new FitAddon.FitAddon();
terminal.loadAddon(fit);
terminal.open(element);
let connection;

function sendSize() {
  if (connection?.readyState === WebSocket.OPEN) {
    connection.send(JSON.stringify({cols: terminal.cols, rows: terminal.rows}));
  }
}

new ResizeObserver(() => { fit.fit(); sendSize(); }).observe(element);
const encoder = new TextEncoder();
terminal.onData(text => {
  if (connection?.readyState !== WebSocket.OPEN) return;
  const data = encoder.encode(text);
  for (let i = 0; i < data.length; i += 32000) connection.send(data.subarray(i, i + 32000));
});

function connect() {
  const ws = connection = socket('/terminal');
  reconnect.hidden = true;
  status.textContent = '接続中';
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { status.textContent = 'SSH 接続済み'; fit.fit(); sendSize(); terminal.focus(); };
  ws.onmessage = event => terminal.write(new Uint8Array(event.data));
  ws.onclose = () => {
    status.textContent = 'SSH 接続が切れました';
    reconnect.hidden = false;
  };
}
reconnect.onclick = connect;
connect();
