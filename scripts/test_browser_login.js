const { spawn } = require('child_process');
const edgePath = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const edge = spawn(edgePath, [
  '--headless',
  '--remote-debugging-port=9222',
  '--no-first-run',
  '--no-default-browser-check',
  '--user-data-dir=C:\\Users\\HP\\AppData\\Local\\Temp\\edge-test-profile-staff'
]);

setTimeout(async () => {
  try {
    const listRes = await fetch('http://127.0.0.1:9222/json/new?http://127.0.0.1:8000/staff', { method: 'PUT' });
    const tab = await listRes.json();
    console.log('Opened tab:', tab.id);

    const ws = new WebSocket(tab.webSocketDebuggerUrl);
    ws.addEventListener('open', () => {
      console.log('CDP connected');
      ws.send(JSON.stringify({ id: 1, method: 'Network.enable' }));
      ws.send(JSON.stringify({ id: 2, method: 'Runtime.enable' }));
      ws.send(JSON.stringify({ id: 3, method: 'Page.enable' }));
    });

    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.method === 'Runtime.consoleAPICalled') {
        console.log('BROWSER CONSOLE:', msg.params.type, msg.params.args.map(a => a.value || a.description).join(' '));
      }
      if (msg.method === 'Network.responseReceived') {
        console.log('NETWORK RESPONSE:', msg.params.response.url, msg.params.response.status);
      }
      if (msg.id === 3) {
        setTimeout(() => {
          console.log('Filling staff form and clicking submit...');
          const evalExpr = "document.getElementById('staff-login-id').value = '25ag1a6767@gmail.com'; document.getElementById('staff-login-pw').value = 'ace@25ag1a67676staff'; document.getElementById('staff-login-submit').click();";
          ws.send(JSON.stringify({ id: 10, method: 'Runtime.evaluate', params: { expression: evalExpr } }));
        }, 1500);
      }
      if (msg.id === 10) {
        setTimeout(() => {
          const checkExpr = "JSON.stringify({ bannerText: document.getElementById('staff-banner')?.textContent, bannerHidden: document.getElementById('staff-banner')?.hidden, loginHidden: document.getElementById('staff-login-section')?.hidden, workspaceHidden: document.getElementById('staff-workspace')?.hidden })";
          ws.send(JSON.stringify({ id: 20, method: 'Runtime.evaluate', params: { expression: checkExpr } }));
        }, 2000);
      }
      if (msg.id === 20) {
        console.log('UI STATE RESULT:', msg.result.result.value);
        edge.kill();
        process.exit(0);
      }
    });
  } catch (e) {
    console.error('Error:', e);
    edge.kill();
    process.exit(1);
  }
}, 1500);
