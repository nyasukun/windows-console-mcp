const guide = document.querySelector('#agent-guide');
const feedback = document.querySelector('#copy-feedback');

export function openAgentGuide(vm = {}) {
  const name = typeof vm.name === 'string' ? vm.name.trim() : '';
  const identified = vm.id ? '対象の vm_id は ' + vm.id + ' です。' :
    name ? 'vm_inventory で名前が ' + JSON.stringify(name) + ' と一致する VM を探し、その vm_id を使ってください。同名が複数なら選択肢を示して確認してください。' :
      'vm_inventory で作成した VM を探し、対象の vm_id を私に確認してください。';
  const prompt = 'Windows Console MCP のツールで操作してください。' + identified + '\n';
  document.querySelector('#agent-target').textContent = vm.id ? [name, vm.id].filter(Boolean).join(' · ') :
    (name || 'VM作成前') + ' · 作成後は一覧からVM ID入りの指示をコピーできます。';
  document.querySelector('#prompt-setup').textContent = prompt +
    'vm_status で状態を調べ、停止中なら vm_start で起動してください。console_screen で Windows セットアップの画面を確認し、表示内容と次に選ぶ項目を日本語で報告してください。ディスク選択・アカウント情報の入力・強制停止が必要になった場合は私の確認を待ってください。';
  document.querySelector('#prompt-operate').textContent = prompt +
    'console_screen で画面を確認し、現在表示されているウィンドウと操作候補を日本語で教えてください。操作は私が指示したものだけを、同じ vm_id を指定して実行してください。';
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
