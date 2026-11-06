const el = id => document.getElementById(id);
function show(books) {
  el('books').replaceChildren(...books.map(book => {
    const item = document.createElement('li'), box = document.createElement('input');
    box.type = 'checkbox'; box.checked = book.finished; box.setAttribute('aria-label', book.title);
    box.onchange = () => run(async () => show(await desktop.action('set_read', {title:book.title, finished:box.checked})));
    item.append(box, document.createTextNode(book.title)); return item;
  }));
}
async function run(fn) {try {await fn();} catch (err) {el('response').textContent = err.message;}}
el('add').onsubmit = event => {event.preventDefault(); void run(async () => {
  show(await desktop.action('add_book', {title:el('title').value})); el('title').value = '';
});};
el('chat').onsubmit = event => {event.preventDefault(); void run(async () => {
  el('response').textContent = '正在处理…';
  el('response').textContent = await desktop.chat(el('question').value);
  show(await desktop.action('list_books', {}));
});};
el('settings').onclick = () => run(() => desktop.settings());
el('source').onclick = () => run(() => desktop.source());
run(async () => show(await desktop.action('list_books', {})));
