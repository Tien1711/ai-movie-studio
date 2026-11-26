const statusLine = document.getElementById('status');
const pairForm = document.getElementById('pair');
const openButton = document.getElementById('open');
const reconnect = document.getElementById('reconnect');

async function call(type, fields = {}) {
  const result = await chrome.runtime.sendMessage({ type, ...fields });
  if (!result?.ok) throw new Error(result?.error || 'Không kết nối được extension.');
  return result.data;
}

function show(state) {
  pairForm.hidden = state.paired;
  openButton.hidden = reconnect.hidden = !state.paired;
  statusLine.textContent = state.paired ? 'Đã ghép nối: ' + state.name : 'Tạo mã trong Cài đặt AI Movie Studio để ghép nối.';
}

call('status').then(show).catch(error => { statusLine.textContent = error.message; });
reconnect.addEventListener('click', () => { pairForm.hidden = false; });
openButton.addEventListener('click', async () => {
  try {
    await call('open');
    window.close();
  } catch (error) { statusLine.textContent = error.message; }
});
pairForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = pairForm.querySelector('button');
  button.disabled = true;
  try {
    await call('pair', {
      name: document.getElementById('name').value.trim(),
      code: document.getElementById('code').value.trim(),
      port: Number(document.getElementById('port').value),
    });
    await call('open');
    window.close();
  } catch (error) {
    statusLine.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
