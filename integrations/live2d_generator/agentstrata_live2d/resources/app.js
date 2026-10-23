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
  $("import-help").hidden = job.status !== "completed";
  const index = phases.indexOf(job.stage);
  document.querySelectorAll("[data-stage]").forEach((el, i) => {
    el.classList.toggle("done", job.status === "completed" || i < index);
    el.classList.toggle("active", job.status === "running" && i === index);
  });
  $("result-message").textContent =
    job.error ||
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
    $("provider").textContent = image + "。 " + layer + "。";
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
    if (job) await show(job);
  } catch (error) {
    notice(error.message);
  }
}
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
