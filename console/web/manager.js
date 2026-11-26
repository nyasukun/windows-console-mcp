import {openAgentGuide} from './agent_guide.js';
import {topologyEditor, topologyGraph} from './topology.js';

const $ = id => document.getElementById(id);
const editor = $('editor');
const fields = $('fields');
let inventory, submit, afterSubmit, busy = false;
let latestRefresh = 0;
const painted = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function action(text, click, {disabled = false, danger = false} = {}) {
  const button = element('button', text, danger ? 'danger' : '');
  button.type = 'button';
  button.disabled = disabled;
  button.onclick = click;
  return button;
}

function message(node, text = '') {
  node.textContent = text;
  node.hidden = !text;
}

async function api(path, method = 'GET', data = {}) {
  const response = await fetch('/api/' + path, method === 'GET' ? {} : {
    method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(result.error || `操作に失敗しました (HTTP ${response.status})`);
  return result;
}

async function refresh({paint = true} = {}) {
  const request = ++latestRefresh;
  try {
    const result = await api('management');
    if (request !== latestRefresh) return;
    inventory = result;
    if (paint && !busy) render();
    message($('message'), inventory.docker_error);
  } catch (error) {
    if (request === latestRefresh) message($('message'), error.message);
  }
}

function setBusy(value) {
  busy = value;
  $('management-controls').disabled = value;
}

async function operate(path, method, data, {confirmation, progress, vmId, onError} = {}) {
  if (busy || (confirmation && !window.confirm(confirmation))) return;
  setBusy(true);
  clearRecovery();
  message($('message'));
  message($('operation'), progress);
  const state = vmId && $('vm-state-' + vmId);
  const previousState = state && [state.textContent, state.className];
  if (state) {
    state.textContent = progress;
    state.className = 'state pending';
  }
  try {
    await api(path, method, data);
    await refresh({paint: false});
  } catch (error) {
    await refresh({paint: false});
    message($('message'), error.message);
    onError?.();
  } finally {
    setBusy(false);
    if (state?.isConnected) [state.textContent, state.className] = previousState;
    render();
    message($('operation'));
  }
}

function clearRecovery() {
  $('recovery').replaceChildren();
  $('recovery').hidden = true;
}

function forceDelete(vm) {
  return operate(`vms/${vm.id}`, 'DELETE', {force: true}, {
    confirmation: `「${vm.name}」の電源を強制的に切り、VMと仮想ディスクを完全に削除します。この操作は元に戻せません。削除しますか？`,
    progress: vm.name + ' を強制停止して削除しています…', vmId: vm.id
  });
}

function offerRecovery(vm) {
  const current = inventory.vms.find(item => item.id === vm.id);
  if (!current || stopped.has(current.status)) return;
  const recovery = $('recovery');
  recovery.append(element('span', '終了を待てない場合は、対象VMを強制停止して削除できます。'),
    action('強制停止して削除', () => forceDelete(current), {danger: true}));
  recovery.hidden = false;
}

function empty(text) {
  return element('p', text, 'empty');
}

function config(vm) {
  return `${vm.vcpus} vCPU · ${vm.ram_mib.toLocaleString()} MiB · ${vm.disk_gib} GiB`;
}

const stopped = new Set(['stopped', 'created', 'exited', 'dead']);
const statuses = {
  running: '稼働中', stopped: '停止中', created: '停止中', exited: '停止中',
  dead: '異常終了', paused: '一時停止', restarting: '再起動中', deleting: '削除処理中', unknown: '不明'
};

function stateBadge(vm) {
  return element('span', statuses[vm.status] || vm.status, `state ${vm.status}`);
}

function serviceState(status) {
  return status ? statuses[status] || status : '状態不明';
}

function renderVMs(topologies) {
  $('vm-count').textContent = inventory.vms.length;
  if (!inventory.vms.length) {
    const onboarding = element('div', undefined, 'onboarding');
    if (inventory.templates.length) {
      onboarding.append(element('p', 'テンプレートから最初の VM を作成できます。'));
    } else if (inventory.images.length) {
      onboarding.append(element('p', '最初に、インストール ISO とマシンサイズを選んでテンプレートを作成します。'));
    } else {
      onboarding.append(element('p', 'ホストの ISO フォルダーにインストール ISO を置き、「更新」を押してください。'));
    }
    $('vms').replaceChildren(onboarding);
    return;
  }
  const table = element('table');
  const header = table.createTHead().insertRow();
  for (const title of ['VM', 'ネットワーク', '状態', '操作']) {
    const th = element('th', title);
    th.scope = 'col';
    header.append(th);
  }
  const rows = table.createTBody();
  for (const vm of inventory.vms) {
    const row = rows.insertRow();
    const identity = row.insertCell();
    identity.append(element('strong', vm.name), element('div', config(vm), 'meta'));
    if (vm.legacy_container) identity.append(element('div', vm.image_id, 'meta'));
    const topology = topologies.get(vm.topology_id);
    const network = row.insertCell();
    network.className = 'vm-network';
    const connection = vm.legacy_container ? 'NAT（' + (vm.legacy_internet ? '有効' : '無効') + '）' : topology || '接続なし';
    network.append(element('span', connection), element('div', vm.segment || '', 'meta'));
    const state = stateBadge(vm);
    state.id = 'vm-state-' + vm.id;
    row.insertCell().append(state);
    const actions = element('div', undefined, 'actions');
    const path = `vms/${vm.id}`;
    if (vm.status === 'running') {
      const consoleLink = element('a', 'コンソール');
      consoleLink.href = `/vms/${vm.id}/console/`;
      actions.append(consoleLink, action('停止', () => operate(path + '/stop', 'POST', {timeout_seconds: 15}, {
        progress: vm.name + ' の Windows の終了を待っています…', vmId: vm.id, onError: () => offerRecovery(vm)
      })));
    }
    if (!stopped.has(vm.status) && vm.status !== 'deleting') {
      const menu = element('details', undefined, 'action-menu');
      const toggle = element('summary', 'その他');
      toggle.setAttribute('aria-label', vm.name + ' のその他の操作');
      menu.append(toggle);
      if (['running', 'paused', 'restarting'].includes(vm.status)) menu.append(action('強制停止', () => {
        menu.open = false;
        operate(path + '/stop', 'POST', {force: true}, {
          confirmation: `「${vm.name}」の電源を強制的に切りますか？保存していないデータは失われます。`,
          progress: vm.name + ' を強制停止しています…', vmId: vm.id
        });
      }, {danger: true}));
      menu.append(action('強制停止して削除', () => {
        menu.open = false;
        forceDelete(vm);
      }, {danger: true}));
      actions.append(menu);
    } else if (stopped.has(vm.status)) {
      actions.append(action('起動', () => operate(path + '/start', 'POST', {}, {
        progress: vm.name + ' を起動しています…', vmId: vm.id
      })));
      if (!vm.legacy_container) actions.append(action('接続先を変更', () => openNetwork(vm)));
    }
    if (vm.status === 'deleting') {
      actions.append(action('削除を再試行', () => forceDelete(vm), {danger: true}));
    } else if (stopped.has(vm.status)) {
      actions.append(action('削除', () => operate(path, 'DELETE', {}, {
        confirmation: `「${vm.name}」と仮想ディスクを完全に削除しますか？`,
        progress: vm.name + ' を削除しています…', vmId: vm.id
      }), {danger: true}));
    }
    if (vm.status !== 'deleting') actions.append(action('AIエージェントで操作', () => openAgentGuide(vm)));
    row.insertCell().append(actions);
  }
  const scroll = element('div', undefined, 'scroll');
  scroll.append(table);
  $('vms').replaceChildren(scroll);
}

function renderCards(id, items, detail, edit, remove, emptyText) {
  $(id).replaceChildren(...(items.length ? items.map(item => {
    const card = element('div', undefined, 'card');
    const content = element('div');
    content.append(element('strong', item.name), ...detail(item).map(line => element('div', line, 'meta')));
    const buttons = element('div', undefined, 'actions');
    buttons.append(action('編集', () => edit(item), {disabled: busy}),
      action('削除', () => remove(item), {disabled: busy, danger: true}));
    card.append(content, buttons);
    return card;
  }) : [empty(emptyText)]));
}

function paintSection(id, state, draw) {
  const signature = JSON.stringify(state);
  if (painted.get(id) === signature) return;
  draw();
  painted.set(id, signature);
}

function render() {
  if (!inventory) return;
  const newVM = $('new-vm');
  const newLabel = inventory.templates.length ? '＋ VM を作成' : '＋ テンプレートを作成';
  if (newVM.textContent !== newLabel) newVM.textContent = newLabel;
  newVM.disabled = busy || (!inventory.templates.length && !inventory.images.length);
  $('new-template').disabled = busy || !inventory.images.length;
  $('new-topology').disabled = busy;
  $('refresh').disabled = busy;
  const topologyNames = inventory.topologies.map(topology => [topology.id, topology.name]);
  paintSection('vms', [inventory.vms, topologyNames, !inventory.vms.length &&
    [!!inventory.templates.length, !!inventory.images.length]], () => renderVMs(new Map(topologyNames)));
  paintSection('templates', [inventory.templates, !!inventory.images.length], () =>
    renderCards('templates', inventory.templates, item => [item.image_id, config(item)], openTemplate,
    item => operate(`templates/${item.id}`, 'DELETE', {}, {
      confirmation: `テンプレート「${item.name}」を削除しますか？`,
      progress: item.name + ' を削除しています…'
    }),
    inventory.images.length ? 'VM を作成するにはテンプレートを追加してください。' :
      'インストール ISO をホストの ISO フォルダーに配置し、更新してください。'));
  paintSection('topologies', [inventory.topologies, inventory.vms], renderTopologies);
}

function renderTopologies() {
  $('topologies').replaceChildren(topologyGraph(inventory, {
    get busy() { return busy; },
    button: action, edit: openTopology, move: openNetwork, state: stateBadge, serviceState,
    canMove: vm => !busy && stopped.has(vm.status),
    attach: (vm, topology_id, segment) => operate(`vms/${vm.id}/network`, 'PUT', {topology_id, segment}, {
      progress: vm.name + ' の接続先を変更しています…', vmId: vm.id
    }),
    remove: removeTopology
  }));
}

function removeTopology(item) {
  const members = inventory.vms.filter(vm => vm.topology_id === item.id);
  if (members.length) {
    message($('message'), '先に接続先を変更してください · ' + members.map(vm => vm.name).join('、'));
    $('message').scrollIntoView({block: 'nearest'});
    return;
  }
  operate(`topologies/${item.id}`, 'DELETE', {}, {
    confirmation: `「${item.name}」を削除しますか？`, progress: item.name + ' を削除しています…'
  });
}

function field(label, control) {
  const checkbox = control.type === 'checkbox';
  const wrapper = element('div', undefined, checkbox ? 'field check' : 'field');
  control.id = 'field-' + fields.childElementCount;
  const caption = element('label', label);
  caption.htmlFor = control.id;
  wrapper.append(...(checkbox ? [control, caption] : [caption, control]));
  fields.append(wrapper);
  return control;
}

function input(label, value, attributes = {}) {
  return field(label, Object.assign(element('input'), {required: true, value}, attributes));
}

function select(label, value, options) {
  const control = element('select');
  for (const [id, text] of options) control.add(new Option(text, id));
  control.value = value;
  return field(label, control);
}

function dialog(title, button, build, endpoint, method, next) {
  if (busy) return;
  fields.replaceChildren();
  $('form-title').textContent = title;
  $('submit').textContent = button;
  $('submit').disabled = false;
  editor.classList.toggle('topology-dialog', endpoint.split('/')[0] === 'topologies');
  afterSubmit = next;
  message($('form-error'));
  const data = build();
  submit = () => api(endpoint, method, data());
  editor.showModal();
}

function openTemplate(item, next) {
  dialog(item ? 'テンプレートを編集' : 'テンプレートを追加', '保存', () => {
    const name = input('名前', item?.name || '', {maxLength: 80});
    const choices = inventory.images.map(image => [image.id, image.id]);
    if (item && !choices.some(([id]) => id === item.image_id)) {
      choices.unshift(['', `${item.image_id}（利用不可）`]);
    }
    const image = select('インストール ISO', item && choices.some(([id]) => id === item.image_id) ?
      item.image_id : choices[0]?.[0], choices);
    image.required = true;
    const cpu = input('vCPU', item?.vcpus || 4, {type: 'number', min: 1, max: 64});
    const ram = input('RAM (MiB)', item?.ram_mib || 8192, {type: 'number', min: 2048, max: 262144});
    const disk = input('ディスク (GiB)', item?.disk_gib || 80, {type: 'number', min: 32, max: 4096});
    return () => ({name: name.value, image_id: image.value, vcpus: cpu.valueAsNumber,
      ram_mib: ram.valueAsNumber, disk_gib: disk.valueAsNumber});
  }, item ? `templates/${item.id}` : 'templates', item ? 'PUT' : 'POST', next);
}

function openTopology(item) {
  const members = item ? inventory.vms.filter(vm => vm.topology_id === item.id) : [];
  dialog(item ? 'トポロジーを編集' : 'トポロジーを作成', '保存', () =>
    topologyEditor(item, members, {
      fields, input, button: action, submitButton: $('submit'), isStopped: vm => stopped.has(vm.status)
    }), item ? `topologies/${item.id}` : 'topologies', item ? 'PUT' : 'POST');
}

function networkFields(vm) {
  const options = [['', '接続なし'], ...inventory.topologies.map(topology => [topology.id, topology.name])];
  const topology = select('トポロジ', vm?.topology_id || '', options);
  const segment = select('セグメント', '', []);
  function populate(value) {
    segment.replaceChildren();
    const target = inventory.topologies.find(item => item.id === topology.value);
    segment.closest('.field').hidden = !target;
    for (const cidr of target?.segments || []) segment.add(new Option(cidr, cidr));
    if (value) segment.value = value;
  }
  topology.onchange = () => populate();
  populate(vm?.segment);
  return () => ({topology_id: topology.value || null, segment: segment.value || null});
}

function openVM() {
  dialog('VM を作成', '作成', () => {
    const name = input('VM 名', '', {maxLength: 80});
    const templates = inventory.templates.map(item => [item.id, `${item.name}（${config(item)}）`]);
    const template = select('テンプレート', templates[0][0], templates);
    const network = networkFields();
    const guide = action('AIエージェントで操作', () => openAgentGuide({name: name.value}));
    guide.classList.add('agent-guide-entry');
    fields.append(guide);
    return () => ({name: name.value, template_id: template.value, ...network()});
  }, 'vms', 'POST');
}

function openNetwork(vm) {
  dialog(`${vm.name} の接続先`, '保存', () => networkFields(vm), `vms/${vm.id}/network`, 'PUT');
}

$('new-vm').onclick = () => {
  if (inventory.templates.length) openVM();
  else openTemplate(undefined, () => { if (inventory.templates.length) openVM(); });
};
$('new-template').onclick = () => openTemplate();
$('new-topology').onclick = () => openTopology();
$('refresh').onclick = refresh;
$('cancel').onclick = () => editor.close();
editor.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
$('editor-form').onsubmit = async event => {
  event.preventDefault();
  if (busy || $('submit').disabled) return;
  setBusy(true);
  fields.inert = true;
  $('submit').disabled = $('cancel').disabled = true;
  message($('form-error'));
  let saved = false;
  try {
    await submit();
    saved = true;
    editor.close();
    await refresh({paint: false});
  } catch (error) {
    message($('form-error'), error.message);
  } finally {
    setBusy(false);
    fields.inert = false;
    $('submit').disabled = $('cancel').disabled = false;
    if (saved) {
      render();
      afterSubmit?.();
    }
  }
};
for (const id of ['new-vm', 'new-template', 'new-topology']) $(id).disabled = true;
refresh();
