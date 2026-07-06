// Панель форматирования для текста рассылки: оборачивает выделенный фрагмент
// в HTML-теги Telegram (<b>, <i>, <u>, <s>, <code>, <tg-spoiler>, <a>).
// Теги работают только с parse_mode = HTML — при других режимах панель гаснет.
(function () {
  "use strict";

  var textarea = document.getElementById("message_text");
  var toolbar = document.getElementById("editor-toolbar");
  var parseMode = document.getElementById("parse_mode");
  var hint = document.getElementById("editor-hint");
  if (!textarea || !toolbar) return;

  // Вставляет открывающий/закрывающий фрагмент вокруг выделения.
  // Если ничего не выделено — ставит пару тегов и курсор между ними.
  function surround(open, close) {
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    var value = textarea.value;
    var selected = value.slice(start, end);
    textarea.value = value.slice(0, start) + open + selected + close + value.slice(end);
    if (selected) {
      textarea.selectionStart = start + open.length;
      textarea.selectionEnd = start + open.length + selected.length;
    } else {
      textarea.selectionStart = textarea.selectionEnd = start + open.length;
    }
    textarea.focus();
  }

  function wrapTag(tag) {
    surround("<" + tag + ">", "</" + tag + ">");
  }

  function insertLink() {
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    var selected = textarea.value.slice(start, end);
    var url = window.prompt("Адрес ссылки (URL):", "https://");
    if (!url) return;
    var text = selected || window.prompt("Текст ссылки:", "") || url;
    var start0 = textarea.value.slice(0, start);
    var rest = textarea.value.slice(end);
    var anchor = '<a href="' + url + '">' + text + "</a>";
    textarea.value = start0 + anchor + rest;
    textarea.selectionStart = textarea.selectionEnd = start + anchor.length;
    textarea.focus();
  }

  toolbar.addEventListener("click", function (e) {
    var btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    if (btn.hasAttribute("data-link")) {
      insertLink();
    } else if (btn.hasAttribute("data-tag")) {
      wrapTag(btn.getAttribute("data-tag"));
    }
  });

  // Панель активна только для HTML-разметки.
  function refreshState() {
    var isHtml = !parseMode || parseMode.value === "HTML";
    toolbar.classList.toggle("disabled", !isHtml);
    var buttons = toolbar.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = !isHtml;
    if (hint) {
      hint.textContent = isHtml
        ? "Выделите текст и нажмите кнопку — она обернёт его в HTML-тег Telegram."
        : "Кнопки форматирования работают только с разметкой HTML.";
    }
  }

  if (parseMode) parseMode.addEventListener("change", refreshState);
  refreshState();
})();
