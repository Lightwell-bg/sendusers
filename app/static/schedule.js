// Конвертирует значение <input type="datetime-local"> (локальное время
// браузера) в UTC-строку "YYYY-MM-DD HH:MM:SS" перед отправкой формы —
// админ вводит своё время, не пересчитывая вручную в серверное (сервер
// работает в UTC). Форма должна иметь data-schedule-form, в ней — поле
// data-local-datetime (видимое, что вводит пользователь) и скрытое поле
// data-utc-datetime (реально уходит на сервер как scheduled_at).
(function () {
  "use strict";

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function toUtcString(localValue) {
    var d = new Date(localValue);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":00";
  }

  document.querySelectorAll("form[data-schedule-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var localInput = form.querySelector("[data-local-datetime]");
      var hidden = form.querySelector("[data-utc-datetime]");
      if (localInput && hidden && localInput.value) {
        hidden.value = toUtcString(localInput.value);
      }
    });
  });
})();
