"use strict";

const header = document.querySelector(".site-header");
const toggle = document.querySelector(".menu-toggle");
const navigation = document.querySelector("#navigation");
if (header && toggle && navigation) {
  header.classList.add("enhanced");
  function closeMenu() {
    navigation.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }
  toggle.addEventListener("click", () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    navigation.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
  });
  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) closeMenu();
  });
  header.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      closeMenu();
      toggle.focus();
    }
  });
}

const video = document.querySelector("#product-video");
const chapters = document.querySelector(".chapter-links");
const videoStatus = document.querySelector("#video-status");
if (video && chapters) {
  chapters.hidden = false;
  chapters.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-time]");
    if (!button) return;
    videoStatus.textContent = "";
    video.currentTime = Number(button.dataset.time);
    try {
      await video.play();
      video.focus();
    } catch {
      videoStatus.textContent = "请点击播放器中的播放按钮。也可以使用下方链接下载视频。";
    }
  });
  video.addEventListener("error", () => {
    videoStatus.textContent = "视频暂时无法播放，请重试或使用下方的下载视频链接。";
  });
}

const copyButton = document.querySelector("#copy-command");
if (copyButton) {
  copyButton.hidden = false;
  copyButton.addEventListener("click", async () => {
    const status = document.querySelector("#copy-status");
    const command = document.querySelector("#install-command");
    try {
      await navigator.clipboard.writeText(command.textContent.trim());
      status.textContent = "命令已复制。请在 Linux / WSL2 终端中运行。";
      copyButton.textContent = "已复制";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "请手动复制已选中的命令。";
    }
  });
}
