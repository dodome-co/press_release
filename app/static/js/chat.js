// AI とのチャット（書類の入力画面）。LINE のような吹き出しで表示する。
// AI の返事は、HTML ではなく文字として表示する。音声入力は、ブラウザの音声認識（Chrome・Edge）を使う。
(function () {
  "use strict";
  var root = document.getElementById("ai-chat");
  if (!root) return;
  var form = root.querySelector(".chat-form");
  var input = root.querySelector("#chat-message");
  var log = root.querySelector(".chat-log");
  var status = root.querySelector(".chat-status");
  var remaining = root.querySelector(".chat-remaining");
  var sendButton = form.querySelector(".chat-send");
  var micButton = form.querySelector(".chat-mic");
  var avatar = root.dataset.avatar;
  var usedVoice = false; // いま入力欄にある文字が、音声で入ったものか

  function clock() {
    // 「午後5:36」の形（LINE と同じ）
    return new Date().toLocaleTimeString("ja-JP", { hour: "numeric", minute: "2-digit", hour12: true });
  }

  function add(role, text) {
    var li = document.createElement("li");
    li.className = "chat-" + role;
    var body = document.createElement("div");
    body.className = "chat-body";
    var bubble = document.createElement("span");
    bubble.className = "chat-bubble";
    bubble.textContent = text; // 文字として入れる（HTML としては扱わない）
    if (role === "ai") {
      var img = document.createElement("img");
      img.className = "chat-avatar";
      img.src = avatar;
      img.alt = "ねたもと。の AI";
      img.width = 40;
      img.height = 40;
      li.appendChild(img);
      body.appendChild(bubble);
      var time = document.createElement("time");
      time.className = "chat-time";
      time.textContent = clock();
      body.appendChild(time);
    } else {
      var read = document.createElement("span");
      read.className = "chat-read";
      read.appendChild(document.createTextNode("既読"));
      read.appendChild(document.createElement("br"));
      read.appendChild(document.createTextNode(clock()));
      body.appendChild(read);
      body.appendChild(bubble);
    }
    li.appendChild(body);
    log.appendChild(li);
    log.scrollTop = log.scrollHeight;
    return li;
  }

  // ---- 音声入力（マイクのボタン）と、AI の質問の読み上げ ----
  var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  var recognizing = null;

  function speak(text) {
    // 音声で答えたときは、AI の質問を、音声でも読み上げる（文字は、そのまま出る）
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    var utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "ja-JP";
    window.speechSynthesis.speak(utterance);
  }

  function setupMic() {
    if (!micButton) return;
    micButton.hidden = false;
    if (!Recognition) {
      micButton.disabled = true;
      micButton.title = "このブラウザは、音声入力に対応していません（Chrome・Edge でお使いください）";
      return;
    }
    if (!window.isSecureContext) {
      // ブラウザは、https:// のページでしか、マイクを使わせない
      micButton.disabled = true;
      micButton.title = "音声入力は、https:// で開いたときに使えます";
      return;
    }
    micButton.addEventListener("click", function () {
      if (recognizing) {
        recognizing.stop();
        return;
      }
      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
      var recognition = new Recognition();
      recognition.lang = "ja-JP";
      recognition.interimResults = true;
      recognition.continuous = false;
      var base = input.value ? input.value.replace(/\s+$/, "") + " " : "";
      recognition.onresult = function (event) {
        var text = "";
        for (var i = 0; i < event.results.length; i++) text += event.results[i][0].transcript;
        input.value = base + text;
        usedVoice = true;
      };
      recognition.onerror = function (event) {
        if (event.error === "not-allowed" || event.error === "service-not-allowed") {
          status.textContent = "マイクが許可されていません。ブラウザの設定で、マイクを許可してください。";
        } else if (event.error === "no-speech") {
          status.textContent = "声が聞こえませんでした。もう一度、マイクのボタンを押してください。";
        } else if (event.error !== "aborted") {
          status.textContent = "音声を聞き取れませんでした。もう一度お試しください。";
        }
      };
      recognition.onend = function () {
        recognizing = null;
        micButton.classList.remove("is-listening");
        micButton.setAttribute("aria-label", "音声で入力する");
        input.focus();
      };
      recognizing = recognition;
      micButton.classList.add("is-listening");
      micButton.setAttribute("aria-label", "音声入力を止める");
      status.textContent = "聞いています…（話し終わったら、「送る」を押してください）";
      recognition.start();
    });
  }
  setupMic();

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
    if (recognizing) recognizing.stop();
    var text = input.value.trim();
    if (!text) {
      status.textContent = "メッセージを入力してください。";
      return;
    }
    var viaVoice = usedVoice;
    sendButton.disabled = true;
    status.textContent = "AI が考えています…";
    add("client", text);
    input.value = "";
    usedVoice = false;

    fetch(root.dataset.url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": root.dataset.csrf },
      body: JSON.stringify({ message: text, input_method: viaVoice ? "voice" : "text" }),
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
          usedVoice = viaVoice;
          status.textContent = result.data.error || "送れませんでした。もう一度お試しください。";
          return;
        }
        add("ai", result.data.reply);
        if (viaVoice) speak(result.data.reply);
        if (remaining) remaining.textContent = result.data.remaining;
        status.textContent = result.data.complete
          ? "AI が、プレスリリースの内容をまとめました。写真を追加して、いちばん下の「プレビューへ」に進んでください。"
          : "";
      })
      .catch(function () {
        log.removeChild(log.lastElementChild);
        input.value = text;
        usedVoice = viaVoice;
        status.textContent = "通信できませんでした。もう一度お試しください。";
      })
      .finally(function () {
        sendButton.disabled = false;
        input.focus();
      });
  });
})();
