document.querySelector('form').onsubmit = async event => {
  event.preventDefault();
  const result = document.querySelector('[role=status]');
  result.textContent = '正在处理…';
  try { result.textContent = await desktop.chat(document.querySelector('input').value); }
  catch (error) { result.textContent = error.message; }
};
document.getElementById('settings').onclick = () => desktop.settings();
