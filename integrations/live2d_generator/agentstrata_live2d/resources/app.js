const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="generator-token"]').content;
const labels = {
  queued: "等待开始",
  running: "生成中",
  completed: "已完成",
  failed: "未完成",
  cancelled: "已取消",
};
const phases = [
  "illustration",
  "decomposition",
  "rigging",
  "validation",
  "packaging",
];
let selected = null,
  busy = false,
  previewUrl = null,
  shownPreview = null;
async function api(path, options = {}) {
  const response = await fetch("/api/" + path, {
    ...options,
    headers: { "x-generator-token": token, ...options.headers },
  });
  if (!response.ok) {
    let message = "请求失败";
    try {
      message = (await response.json()).error || message;
    } catch {}
    throw new Error(message);
  }
  return response;
}
function notice(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
function inputState() {
  const f = $("file").files[0];
  $("file-label").textContent = f ? f.name : "选择一份素材";
  $("clear-file").hidden = !f;
  $("redraw").disabled = !f || /\.(psd|zip)$/i.test(f.name);
  if ($("redraw").disabled) $("redraw").checked = false;
}
$("file").addEventListener("change", inputState);
$("clear-file").onclick = () => {
  $("file").value = "";
  inputState();
};
$("example").onclick = () => {
  $("name").value = "ヰ世界情緒 · 参考创作";
  $("prompt").value =
    "依据ヰ世界情緒的形象进行参考创作：银白色长发，侧刘海遮住一只眼睛，灰绿色眼睛，向日葵发饰，白色与明黄色长外套，深色三角形扣饰与黑色长袜。温柔、安静，有异世界的神秘感。正面全身立绘，双臂略微离开身体，白色背景。";
  $("information").value = JSON.stringify(
    {
      参考来源: "https://kamitsubaki.jp/artist/isekaijoucho/",
      用途: "个人 Live2D 形象练习",
    },
    null,
    2,
  );
};
function encode(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}
$("form").onsubmit = async (event) => {
  event.preventDefault();
  if (busy) return;
  notice("");
  $("generate").disabled = true;
  try {
    const file = $("file").files[0];
    if (file && file.size > 48 * 1024 * 1024)
      throw new Error("文件超过 48 MiB");
    let information = {};
    try {
      information = JSON.parse($("information").value || "{}");
    } catch {
      throw new Error("角色资料须为合法 JSON 文本键值表");
    }
    const payload = {
      request: {
        name: $("name").value,
        prompt: $("prompt").value,
        information,
        redraw: $("redraw").checked,
      },
    };
    if ($("generation-character").value) payload.character_id = $("generation-character").value;
    if (file) payload.input = { filename: file.name, data: await encode(file) };
    const job = await (
      await api("jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      })
    ).json();
    selected = job.id;
    shownPreview = null;
    await refresh();
  } catch (error) {
    notice(error.message);
  } finally {
    $("generate").disabled = busy;
  }
};
async function show(job) {
  $("badge").textContent = labels[job.status] || job.status;
  $("result-title").textContent = job.name;
  $("cancel").hidden = !["queued", "running"].includes(job.status);
  $("download").hidden = job.status !== "completed";
  $("collect-job").hidden = job.status !== "completed";
  $("retry-job").hidden = !["failed", "cancelled"].includes(job.status);
  $("retry-job").disabled = busy;
  $("retry-advice").hidden = $("retry-job").hidden;
  $("retry-advice").textContent = (job.retry_at
    ? `服务建议重试时间：${new Date(job.retry_at).toLocaleString()}。` : "") +
    "重试会创建新任务，保留原失败记录；拆层失败时复用已生成的立绘。";
  $("import-help").hidden = job.status !== "completed";
  const index = phases.indexOf(job.stage);
  document.querySelectorAll("[data-stage]").forEach((el, i) => {
    el.classList.toggle("done", job.status === "completed" || i < index);
    el.classList.toggle("active", job.status === "running" && i === index);
  });
  $("result-message").textContent =
    job.error || job.library_error ||
    (job.status === "completed"
      ? `已生成 ${job.result.generation.layers} 个图层、${job.result.generation.parameters} 个参数。模型包已通过文件完整性检查；这是可继续精修的自动绑定初稿。`
      : "生成通常需要几分钟，可保持页面打开或稍后回来查看。");
  if (job.status === "completed" && shownPreview !== job.id) {
    const response = await api(`jobs/${job.id}/preview.png`);
    const blob = await response.blob();
    if (selected !== job.id) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("preview").src = previewUrl;
    $("preview").hidden = false;
    $("empty").hidden = true;
    shownPreview = job.id;
  }
  if (job.status !== "completed") {
    $("preview").hidden = true;
    $("empty").hidden = false;
  }
}
async function refresh() {
  try {
    const state = await (await api("status")).json();
    busy = state.jobs.some((j) => ["running", "queued"].includes(j.status));
    $("generate").disabled = busy;
    $("source-status").textContent = state.legal.source_configured
      ? ""
      : " · 对应源码尚未配置";
    const caps = state.capabilities;
    const image = caps.image_service
      ? `绘图：${caps.image_model} · ${caps.image_service}`
      : "绘图服务未配置，可上传立绘/分层素材";
    const layer = caps.decompose_service
      ? `拆层：${caps.decompose_service}`
      : "拆层服务未配置，可上传 PSD / ZIP";
    const auth = caps.decompose_provider === "space"
      ? caps.decompose_key_configured ? " 已配置 Hugging Face 凭据，配额由该账户决定。"
        : " 当前匿名调用，受公共 GPU 配额限制；可在本机配置 Hugging Face 凭据。"
      : "";
    $("provider").textContent = image + "。 " + layer + "。" + auth;
    const list = $("jobs");
    list.replaceChildren();
    if (!state.jobs.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "暂无生成记录";
      list.append(p);
    }
    for (const job of state.jobs.slice(0, 20)) {
      const button = document.createElement("button");
      button.className = "job";
      button.type = "button";
      const name = document.createElement("strong"),
        status = document.createElement("span");
      name.textContent = job.name;
      status.textContent =
        (labels[job.status] || job.status) + " · " + job.id.slice(0, 8);
      button.append(name, status);
      button.onclick = () => {
        selected = job.id;
        show(job).catch((e) => notice(e.message));
      };
      list.append(button);
    }
    if (!selected && state.jobs.length) selected = state.jobs[0].id;
    const job = state.jobs.find((j) => j.id === selected);
    if (job) {
      await show(job);
      const saved = `${job.id}:${job.character_version || 0}`;
      if (job.character_version && lastSavedCharacter !== saved) {
        lastSavedCharacter = saved;
        await refreshCharacters();
      }
    }
  } catch (error) {
    notice(error.message);
  }
}
$("retry-job").onclick = async () => {
  if (!selected || busy) return;
  $("retry-job").disabled = true;
  notice("");
  try {
    const job = await (await api(`jobs/${selected}/retry`, {method: "POST"})).json();
    selected = job.id;
    shownPreview = null;
    await refresh();
  } catch (error) {
    notice(error.message);
    $("retry-job").disabled = busy;
  }
};
$("cancel").onclick = async () => {
  if (!selected) return;
  try {
    await api(`jobs/${selected}/cancel`, { method: "POST" });
    await refresh();
  } catch (e) {
    notice(e.message);
  }
};
$("download").onclick = async () => {
  if (!selected) return;
  try {
    const response = await api(`jobs/${selected}/model.zip`);
    const url = URL.createObjectURL(await response.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `live2d-${selected.slice(0, 8)}.zip`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) {
    notice(e.message);
  }
};
refresh();
setInterval(refresh, 2500);


let currentCharacter = null, characterPreviewUrl = null, lastSavedCharacter = null;
const presetUrls = [];
const profileFields = {name: "character-name", description: "character-description", identity: "character-identity",
  response_style: "character-style", author: "character-author", license: "character-license", source: "character-source"};
function profileInput() {
  const value = Object.fromEntries(Object.entries(profileFields).map(([key, id]) => [key, $(id).value]));
  value.voice = {provider: "edge_tts", voice: $("character-voice").value};
  return value;
}
function showProfile(value) {
  for (const [key, id] of Object.entries(profileFields)) $(id).value = value[key] || "";
  $("character-voice").value = value.voice.voice;
}
async function jsonApi(path, value = {}) {
  return (await api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)})).json();
}
async function action(button, fn) {
  button.disabled = true;
  notice("");
  try { await fn(); } catch (error) { notice(error.message); }
  finally { button.disabled = false; }
}
function switchView(name) {
  for (const view of ["generation", "library", "presets"]) $(view + "-view").hidden = view !== name;
  document.querySelectorAll("[data-view]").forEach(button => {
    if (button.dataset.view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}
document.querySelectorAll("[data-view]").forEach(button => button.onclick = () => {
  switchView(button.dataset.view);
  const work = button.dataset.view === "presets" ? refreshPresets() : refreshCharacters();
  work.catch(error => notice(error.message));
});
async function refreshCharacters() {
  const {characters} = await (await api("characters")).json();
  const previous = $("generation-character").value;
  $("generation-character").replaceChildren(new Option("新建角色", ""));
  const list = $("characters");
  list.replaceChildren();
  if (!characters.length) list.textContent = "还没有角色。新建角色、开始生成或导入已有预设。";
  for (const item of characters) {
    const button = document.createElement("button"), title = document.createElement("strong"), detail = document.createElement("small");
    button.type = "button";
    button.setAttribute("aria-current", String(currentCharacter?.id === item.id));
    title.textContent = item.draft.name;
    detail.textContent = `${item.versions.length} 个版本 · ${item.read_only ? "预设" : "本地作品"} · ${item.id.slice(0, 8)}`;
    button.append(title, detail);
    button.onclick = () => showCharacter(item.id).catch(error => notice(error.message));
    list.append(button);
    if (!item.read_only) $("generation-character").append(new Option(item.draft.name + " · " + item.id.slice(0, 8), item.id));
  }
  if ([...$("generation-character").options].some(option => option.value === previous)) $("generation-character").value = previous;
}
async function showCharacter(identifier, selectedVersion = null) {
  const item = await (await api(`characters/${identifier}`)).json();
  currentCharacter = item;
  const versions = $("character-version");
  versions.replaceChildren();
  for (const value of [...item.versions].reverse()) versions.append(new Option(`版本 ${value}`, String(value)));
  versions.disabled = !item.versions.length;
  if (selectedVersion !== null) versions.value = String(selectedVersion);
  $("character-fields").disabled = item.read_only;
  $("save-version").disabled = !item.versions.length;
  for (const id of ["fork-character", "export-character", "apply-character"]) $(id).disabled = !item.versions.length;
  $("character-status").textContent = `${item.id} · ${item.read_only ? "预设版本只读，创建副本后编辑" : "编辑草稿；保存新版本会保留所选版本的模型"}`;
  showProfile(item.draft);
  if (item.versions.length) {
    const version = Number(versions.value);
    if (item.read_only || selectedVersion !== null) showProfile((await (await api(`characters/${identifier}/versions/${version}`)).json()).profile);
    const image = await api(`characters/${identifier}/versions/${version}/preview.png`);
    if (currentCharacter?.id !== identifier || Number(versions.value) !== version) return;
    if (characterPreviewUrl) URL.revokeObjectURL(characterPreviewUrl);
    characterPreviewUrl = URL.createObjectURL(await image.blob());
    $("character-preview").src = characterPreviewUrl;
    $("character-preview").hidden = false;
  } else $("character-preview").hidden = true;
  await refreshCharacters();
}
$("character-version").onchange = () => {
  if (currentCharacter) showCharacter(currentCharacter.id, Number($("character-version").value)).catch(error => notice(error.message));
};
$("new-character").onclick = () => action($("new-character"), async () => {
  const item = await jsonApi("characters", {profile: {name: "我的新角色", identity: ""}});
  await showCharacter(item.id);
  $("generation-character").value = item.id;
  $("character-name").focus();
});
$("character-form").onsubmit = event => {
  event.preventDefault();
  if (!currentCharacter) return;
  action($("save-draft"), async () => {
    await jsonApi(`characters/${currentCharacter.id}/draft`, {profile: profileInput()});
    await refreshCharacters();
    notice("草稿已保存；现有版本保持原样。");
  });
};
$("save-version").onclick = () => action($("save-version"), async () => {
  if (!currentCharacter) return;
  const identifier = currentCharacter.id, base = Number($("character-version").value);
  await jsonApi(`characters/${identifier}/draft`, {profile: profileInput()});
  const saved = await jsonApi(`characters/${identifier}/versions`, {base_version: base});
  await showCharacter(identifier, saved.version);
  notice(`已保存版本 ${saved.version}。`);
});
$("collect-job").onclick = () => action($("collect-job"), async () => {
  if (!selected) return;
  const saved = await jsonApi("characters/collect", {job: selected});
  switchView("library");
  await showCharacter(saved.id, saved.version);
});
async function uploadInput(file) {
  if (!file || file.size > 48 * 1024 * 1024) throw new Error("请选择不超过 48 MiB 的 ZIP");
  return {filename: file.name, data: await encode(file)};
}
$("model-upload").onchange = () => action($("model-upload"), async () => {
  if (!currentCharacter || !$("model-upload").files.length) return;
  const identifier = currentCharacter.id;
  await jsonApi(`characters/${identifier}/draft`, {profile: profileInput()});
  const saved = await jsonApi("characters/import-model", {id: identifier, input: await uploadInput($("model-upload").files[0])});
  $("model-upload").value = "";
  await showCharacter(identifier, saved.version);
});
$("preset-upload").onchange = () => action($("preset-upload"), async () => {
  if (!$("preset-upload").files.length) return;
  const saved = await jsonApi("characters/import-preset", {input: await uploadInput($("preset-upload").files[0])});
  $("preset-upload").value = "";
  await showCharacter(saved.id, saved.version);
});
function versionPath() {
  if (!currentCharacter || !$("character-version").value) throw new Error("请先选择角色版本");
  return `characters/${currentCharacter.id}/versions/${$("character-version").value}`;
}
$("fork-character").onclick = () => action($("fork-character"), async () => {
  const saved = await jsonApi(versionPath() + "/fork");
  await showCharacter(saved.id, saved.version);
});
$("export-character").onclick = () => action($("export-character"), async () => {
  const response = await api(versionPath() + "/export", {method: "POST"});
  const url = URL.createObjectURL(await response.blob()), link = document.createElement("a");
  link.href = url;
  link.download = `character-${currentCharacter.id.slice(0, 8)}-v${$("character-version").value}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});
$("apply-character").onclick = () => action($("apply-character"), async () => {
  versionPath();
  const desktop = window.agentstrataDesktop;
  const result = desktop ? await desktop.apply(currentCharacter.id, Number($("character-version").value)) :
    await jsonApi(versionPath() + "/apply");
  if (result.backup) $("application-backup").value = result.backup;
  if (result.ok === false) throw new Error(result.message);
  $("application-backup").value = result.backup;
  notice(`角色已应用，${desktop ? "客户端已重新连接" : "可以启动 Live2D 客户端"}。撤回备份：${result.backup}`);
});
$("restore-application").onclick = () => action($("restore-application"), async () => {
  const backup = $("application-backup").value.trim(), desktop = window.agentstrataDesktop;
  const result = desktop ? await desktop.restore(backup) : await jsonApi("characters/restore-application", {backup});
  if (result.ok === false) throw new Error(result.message);
  notice(desktop ? "已恢复应用前配置，客户端已重新连接。" : "已恢复应用前配置，可以启动 Live2D 客户端。");
});
async function refreshPresets() {
  const result = await (await api("presets")).json();
  for (const url of presetUrls.splice(0)) URL.revokeObjectURL(url);
  $("catalog-status").textContent = !result.configured ? "尚未配置私有预设仓库；可以先导入本地预设。" :
    result.cached ? "当前显示本机缓存。刷新目录可检查新版本。" : "预设源已配置，点击刷新目录获取角色。";
  const list = $("presets");
  list.replaceChildren();
  if (!result.entries.length) list.textContent = "目录中暂无预设。";
  for (const entry of result.entries) {
    const card = document.createElement("article"), image = document.createElement("img"), heading = document.createElement("h3"),
      description = document.createElement("p"), license = document.createElement("p"), button = document.createElement("button");
    card.className = "card preset-card";
    image.alt = entry.name + "预览";
    image.hidden = true;
    heading.textContent = `${entry.name} · v${entry.version}`;
    description.textContent = entry.description;
    license.className = "hint";
    license.textContent = `${entry.author} · ${entry.license} · ${(entry.size / 1024 / 1024).toFixed(1)} MiB`;
    button.type = "button";
    button.className = "primary";
    const installed = result.installed[entry.id]?.includes(entry.version);
    button.textContent = installed ? "查看本地版本" : "下载此版本";
    button.onclick = () => action(button, async () => {
      if (!installed) await jsonApi(`presets/${entry.id}/${entry.version}/download`);
      switchView("library");
      await showCharacter(entry.id, entry.version);
    });
    card.append(image, heading, description, license, button);
    list.append(card);
    api(`presets/${entry.id}/${entry.version}/preview.png`).then(response => response.blob()).then(blob => {
      if (!card.isConnected) return;
      const url = URL.createObjectURL(blob);
      presetUrls.push(url);
      image.src = url;
      image.hidden = false;
    }).catch(() => { image.alt = "预览未缓存或暂不可用"; });
  }
}
$("refresh-catalog").onclick = () => action($("refresh-catalog"), async () => {
  await jsonApi("presets/refresh");
  await refreshPresets();
});
refreshCharacters().catch(error => notice(error.message));
