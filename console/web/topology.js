const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};

function stage(text, detail, className) {
  const node = element('div', undefined, 'graph-node ' + className);
  node.append(element('strong', text), element('span', detail, 'meta'));
  return node;
}

function upstream(state) {
  const nodes = element('div', undefined, 'graph-uplink');
  nodes.append(stage('インターネット', '公開 IPv4・TCP / DNS', 'external-node'),
    stage('透過プロキシ', state, 'proxy-node'));
  return nodes;
}

export function topologyGraph(inventory, ui) {
  const view = document.createDocumentFragment();
  if (!inventory.topologies.length) {
    view.append(element('p', 'セグメントを作り、VMやコンテナを同じネットワークに接続できます。', 'empty'));
    return view;
  }

  const connections = new Map();
  const detached = [];
  const devices = [...inventory.vms, ...(inventory.endpoints || [])];
  for (const vm of devices) {
    if (vm.legacy_container) continue;
    if (!vm.topology_id) {
      detached.push(vm);
      continue;
    }
    const key = vm.topology_id + '/' + vm.segment;
    if (!connections.has(key)) connections.set(key, []);
    connections.get(key).push(vm);
  }

  function vmNode(vm) {
    const node = element('li', undefined, 'graph-vm');
    const changeable = ui.canMove(vm);
    if (changeable) {
      node.draggable = true;
      node.ondragstart = event => {
        event.dataTransfer.setData('application/x-windows-console-vm', vm.id);
        event.dataTransfer.effectAllowed = 'move';
      };
    }
    node.append(element('strong', vm.name), ui.state(vm));
    if (vm.kind) node.append(element('span', [vm.kind === 'browser' ? 'Chrome' : 'Linux', vm.address]
      .filter(Boolean).join(' · '), 'meta'));
    if (changeable) {
      const button = ui.button('接続先を変更', () => ui.move(vm));
      button.setAttribute('aria-label', vm.name + ' の接続先を変更');
      node.append(button);
    }
    return node;
  }

  function members(vms, emptyText = '接続中の機器はありません') {
    if (!vms.length) return element('p', emptyText, 'meta');
    const list = element('ul', undefined, 'graph-vms');
    list.append(...vms.map(vmNode));
    return list;
  }

  const vmById = new Map(devices.filter(vm => !vm.legacy_container).map(vm => [vm.id, vm]));
  function dropTarget(board, topologyId, segment) {
    function isVMDrag(event) {
      return !ui.busy && event.dataTransfer.types.includes('application/x-windows-console-vm');
    }
    board.ondragover = event => {
      if (!isVMDrag(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      board.classList.add('drop-active');
    };
    board.ondragleave = event => {
      if (!board.contains(event.relatedTarget)) board.classList.remove('drop-active');
    };
    board.ondrop = event => {
      board.classList.remove('drop-active');
      if (!isVMDrag(event)) return;
      event.preventDefault();
      const vm = vmById.get(event.dataTransfer.getData('application/x-windows-console-vm'));
      if (vm && ui.canMove(vm) && (vm.topology_id !== topologyId || vm.segment !== segment)) {
        ui.attach(vm, topologyId, segment);
      }
    };
  }

  view.append(element('p', '停止中のVMやコンテナは、ドラッグまたは「接続先を変更」で移動できます。', 'graph-help meta'));
  for (const topology of inventory.topologies) {
    const board = element('section', undefined, 'topology-board');
    board.setAttribute('aria-label', topology.name);
    const heading = element('div', undefined, 'graph-heading');
    const buttons = element('div', undefined, 'actions');
    const powered = topology.router_status === 'running';
    buttons.append(ui.button('構成を編集', () => ui.edit(topology), {disabled: ui.busy}),
      ui.button('DNS 設定', () => ui.dns(topology), {disabled: ui.busy}),
      ui.button(powered ? 'ネットワークを停止' : 'ネットワークを起動',
        () => ui.power(topology, !powered), {disabled: ui.busy}),
      ui.button('削除', () => ui.remove(topology), {disabled: ui.busy, danger: true}));
    heading.append(element('h3', topology.name), buttons);
    const graph = element('div', undefined, 'graph-tree');
    if (topology.proxy) {
      graph.append(upstream(ui.serviceState(topology.proxy_status)));
      graph.classList.add('has-uplink');
    }
    const routerText = [
      ui.serviceState(topology.router_status), 'DHCP・セグメント間ルーティング',
      ...(!topology.proxy ? ['外部接続なし'] : [])
    ].join(' · ');
    graph.append(stage('ルーター', routerText, 'router-node'));
    if (topology.dns != null) {
      graph.append(stage('DNS', ui.serviceState(topology.dns_status) + ' · ' + topology.dns.length + ' 件',
        'dns-node'));
    }
    const branches = element('div', undefined, 'graph-branches');
    for (const cidr of topology.segments) {
      const vms = connections.get(topology.id + '/' + cidr) || [];
      const segment = element('section', undefined, 'graph-segment');
      segment.setAttribute('aria-label', cidr);
      const title = element('div', undefined, 'segment-heading');
      title.append(element('h4', cidr), element('span', vms.length + ' 台', 'meta'));
      segment.append(title, members(vms));
      dropTarget(segment, topology.id, cidr);
      branches.append(segment);
    }
    graph.append(branches);
    board.append(heading, graph);
    view.append(board);
  }

  const disconnected = element('section', undefined, 'graph-segment graph-detached');
  disconnected.setAttribute('aria-label', '未接続の機器');
  disconnected.append(element('h3', '未接続の機器'), members(detached, '未接続の機器はありません'));
  dropTarget(disconnected, null, null);
  view.append(disconnected);
  return view;
}

export function topologyEditor(item, members, ui) {
  const name = ui.input('トポロジー名', item?.name || '', {maxLength: 80});
  const graph = element('div', undefined, 'graph-tree topology-draft');
  const route = upstream('設定をプレビュー中');
  route.hidden = true;
  graph.append(route, stage('ルーター', 'DHCP・セグメント間ルーティング', 'router-node'));
  const proxy = element('input');
  proxy.type = 'checkbox';
  proxy.checked = item?.proxy || false;
  const proxyLabel = element('label', undefined, 'draft-proxy');
  proxyLabel.append(proxy, ' すべてのセグメントの外部接続に透過プロキシを使う');
  graph.firstElementChild.before(proxyLabel);
  const branches = element('div', undefined, 'graph-branches');
  const rows = [];
  const add = ui.button('＋ セグメントを追加', () => addSegment('', true));
  const impact = element('p', undefined, 'topology-impact');
  impact.setAttribute('role', 'status');
  const running = members.filter(vm => !ui.isStopped(vm));

  function update() {
    const values = rows.map(row => row.input.value.trim());
    const changed = item && (proxy.checked !== item.proxy || values.length !== item.segments.length ||
      values.some((value, i) => value !== item.segments[i]));
    const displaced = members.filter(vm => !values.includes(vm.segment));
    const blockers = [];
    if (displaced.length) blockers.push('セグメントから先に移動してください · ' + displaced.map(vm => vm.name).join('、'));
    if (changed && running.length) blockers.push('設定を変更するには先に停止してください · ' + running.map(vm => vm.name).join('、'));
    impact.textContent = blockers.join('\n');
    impact.hidden = !blockers.length;
    ui.submitButton.disabled = blockers.length > 0;
    route.hidden = !proxy.checked;
    graph.classList.toggle('has-uplink', proxy.checked);
    add.disabled = rows.length >= 8;
    for (const row of rows) row.remove.disabled = rows.length === 1;
  }

  function addSegment(value, focus = false) {
    const branch = element('div', undefined, 'graph-segment draft-segment');
    const field = element('input');
    Object.assign(field, {value, required: true, placeholder: '10.40.0.0/24', spellcheck: false});
    const label = element('label', 'セグメントの IPv4 CIDR');
    label.append(field);
    const remove = ui.button('セグメントを削除', () => {
      rows.splice(rows.indexOf(row), 1);
      branch.remove();
      update();
      add.focus();
    });
    remove.classList.add('danger');
    const row = {input: field, remove};
    rows.push(row);
    field.oninput = update;
    branch.append(label, remove);
    branches.append(branch);
    update();
    if (focus) field.focus();
  }

  proxy.onchange = update;
  graph.append(branches, add, impact);
  ui.fields.append(graph);
  for (const cidr of item?.segments || ['']) addSegment(cidr);
  return () => ({name: name.value, segments: rows.map(row => row.input.value.trim()), proxy: proxy.checked});
}
