// AI とのチャット（書類の入力画面）。AI の返事は、HTML ではなく文字として表示する。
(function () {
  "use strict";
  var root = document.getElementById("ai-chat");
  if (!root) return;
  var form = root.querySelector(".chat-form");
  var input = root.querySelector("#chat-message");
  var log = root.querySelector(".chat-log");
  var status = root.querySelector(".chat-status");
  var remaining = root.querySelector(".chat-remaining");
  var button = form.querySelector("button");

  function add(role, text) {
    var li = document.createElement("li");
    li.className = "chat-" + role;
    li.textContent = text;
    log.appendChild(li);
    log.scrollTop = log.scrollHeight;
  }

  // Enter で送信、Shift+Enter で改行（日本語の変換中の Enter は、送信しない）
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  // 開いたときに、いちばん新しい発言まで、スクロールする
  log.scrollTop = log.scrollHeight;

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) {
      status.textContent = "メッセージを入力してください。";
      return;
    }
    button.disabled = true;
    status.textContent = "AI が考えています…";
    add("client", text);
    input.value = "";

    fetch(root.dataset.url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": root.dataset.csrf },
      body: JSON.stringify({ message: text, input_method: "text" }),
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.json().then(
          function (data) { return { ok: response.ok, data: data }; },
          function () { return { ok: false, data: {} }; }
        );
      })
      .then(function (result) {
        if (!result.ok) {
          // 送れなかったので、表示した発言を消して、入力欄に戻す（もう一度送れるように）
          log.removeChild(log.lastElementChild);
          input.value = text;
          status.textContent = result.data.error || "送れませんでした。もう一度お試しください。";
          return;
        }
        add("ai", result.data.reply);
        if (remaining) remaining.textContent = result.data.remaining;
        status.textContent = result.data.complete
          ? "AI が、プレスリリースの内容をまとめました。写真を追加して、いちばん下の「プレビューへ」に進んでください。"
          : "";
      })
      .catch(function () {
        log.removeChild(log.lastElementChild);
        input.value = text;
        status.textContent = "通信できませんでした。もう一度お試しください。";
      })
      .finally(function () {
        button.disabled = false;
        input.focus();
      });
  });
})();
