const guide = document.querySelector('#agent-guide');
const feedback = document.querySelector('#copy-feedback');

export function openAgentGuide(vm = {}) {
  const name = typeof vm.name === 'string' ? vm.name.trim() : '';
  const setup = document.querySelector('#prompt-setup');
  const copySetup = document.querySelector('#copy-setup');
  const target = document.querySelector('#agent-target');
  document.querySelector('#agent-operate').hidden = !vm.id;
  if (vm.id) {
    const prompt = 'Windows Console MCP のツールで操作してください。対象の vm_id は ' + vm.id + ' です。\n';
    target.textContent = [name, vm.id].filter(Boolean).join(' · ');
    setup.textContent = prompt +
      'vm_status で状態を調べ、停止中なら vm_start で起動してください。console_screen で Windows セットアップの画面を確認し、表示内容と次に選ぶ項目を日本語で報告してください。ディスク選択・アカウント情報の入力・強制停止が必要になった場合は私の確認を待ってください。';
    document.querySelector('#prompt-operate').textContent = prompt +
      'console_screen で画面を確認し、現在表示されているウィンドウと操作候補を日本語で教えてください。操作は私が指示したものだけを、同じ vm_id を指定して実行してください。';
    copySetup.textContent = '起動・セットアップ確認の指示をコピー';
  } else {
    target.textContent = name ? 'VM作成前 · ' + name : 'VM作成前';
    const selection = vm.templateId
      ? 'フォームで選んだ template_id は ' + vm.templateId + '、ネットワーク設定は ' + JSON.stringify(vm.network) + ' です。'
      : '使えるテンプレートと ISO を示し、使うテンプレートまたは ISO とネットワークを私に確認してください。テンプレートがなければ、確認した ISO とサイズで vm_template_save を使って作成してください。ISO がなければ、ホストの ISO フォルダーへの配置を案内してください。';
    setup.textContent = 'Windows Console MCP で新規 Windows VM を作成したいです。まず vm_inventory で既存 VM、テンプレート、利用可能な ISO を調べてください。\n' +
      (name ? '新規 VM 名は ' + JSON.stringify(name) + ' です。' : 'VM 名は私に確認してください。') + '\n' + selection + '\n' +
      '同名の VM がある場合は新規作成する前に確認してください。構成が決まったら vm_create で作成し、vm_start で起動してください。作成した vm_id を報告し、同じ ID を console_screen に指定してセットアップ画面と次の選択肢を日本語で説明してください。ディスクやアカウントの選択は私の確認を待ってください。';
    copySetup.textContent = '最初のVMを作成する指示をコピー';
  }
  feedback.textContent = '';
  guide.showModal();
}

guide.querySelector('form').addEventListener('submit', () => { feedback.textContent = ''; });
for (const button of guide.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(source.textContent);
      feedback.textContent = 'コピーしました。';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(source);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      feedback.textContent = '文字列を選択しました。Ctrl+C または Command+C でコピーできます。';
    }
  });
}
