const layer = document.getElementById('walkthrough');
const caption = document.getElementById('demo-caption');
const text = document.getElementById('demo-text');
const pointer = document.getElementById('demo-pointer');
const bar = document.getElementById('demo-time');
let step, timer, animation, deadline;

function placeCaption() {
  if (!step || caption.hidden) return;
  const w = layer.clientWidth, h = layer.clientHeight;
  caption.style.left = Math.max(0, Math.min(w - caption.offsetWidth, w * step.x / 32767)) + 'px';
  caption.style.top = Math.max(0, Math.min(h - caption.offsetHeight, h * step.y / 32767)) + 'px';
}

function placePointer() {
  if (!step) return;
  const [x, y] = step.pointer;
  pointer.style.left = (100 * x / 32767) + '%';
  pointer.style.top = (100 * y / 32767) + '%';
  pointer.style.setProperty('--face-x', (1 - x/32767) * layer.clientWidth < 16 ? -1 : 1);
  pointer.style.setProperty('--face-y', (1 - y/32767) * layer.clientHeight < 21 ? -1 : 1);
}

function dismiss() {
  clearTimeout(timer);
  animation?.cancel();
  animation = null;
  step = null;
  layer.hidden = true;
  text.textContent = '';
}

function show(next) {
  dismiss();
  if (!next || next.remaining_ms <= 0) return;
  step = next;
  const remaining = Math.min(step.remaining_ms, step.duration_ms);
  layer.hidden = false;
  caption.hidden = !step.text;
  text.textContent = step.text;
  placePointer();
  placeCaption();
  if (!caption.hidden) {
    animation = bar.animate([
      {transform: 'scaleX(' + remaining / step.duration_ms + ')'},
      {transform: 'scaleX(0)'}
    ], {duration: remaining, easing: 'linear', fill: 'forwards'});
  }
  deadline = performance.now() + remaining;
  checkExpiry();
}

function checkExpiry() {
  if (!step) return;
  clearTimeout(timer);
  const remaining = deadline - performance.now();
  if (remaining <= 0) dismiss();
  else timer = setTimeout(checkExpiry, Math.min(remaining, 2147483647));
}

document.addEventListener('visibilitychange', checkExpiry);

// The overlay uses the already scaled guest frame as its coordinate space.
const resize = new ResizeObserver(() => {
  placeCaption();
  placePointer();
});
resize.observe(layer);
resize.observe(caption);
const feed = new EventSource('/walkthrough');
feed.addEventListener('step', event => show(JSON.parse(event.data)));
feed.addEventListener('pointer', event => {
  if (!step) return;
  step.pointer = JSON.parse(event.data);
  placePointer();
});
