/* 对话 + 会议上传，无框架、无构建。 */
(function () {
  "use strict";

  /* ---------------- 公共 ---------------- */
  var sessionId = sessionStorage.getItem("aiwainao_session") || "";
  if (!sessionId) {
    sessionId = "sess-" + Math.random().toString(16).slice(2, 10);
    sessionStorage.setItem("aiwainao_session", sessionId);
  }
  document.getElementById("chat-session").textContent = sessionId;

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---------------- 依赖状态 ---------------- */
  fetch("/api/health")
    .then(function (r) { return r.json(); })
    .then(function (h) {
      var box = document.getElementById("badges");
      box.innerHTML = "";
      function add(text, cls) {
        var span = document.createElement("span");
        span.className = "badge " + cls;
        span.textContent = text;
        box.appendChild(span);
      }
      add("DB " + (h.db.ok ? "OK" : "FAIL"), h.db.ok ? "ok" : "bad");
      add("LLM " + h.llm.mode, h.llm.ok ? (h.llm.mode === "mock" ? "warn" : "ok") : "bad");
      if (h.search) {
        add("SEARCH " + h.search.mode, h.search.ok ? (h.search.mode === "mock" ? "warn" : "ok") : "bad");
      }
      add("MAIL " + h.smtp.mode, h.smtp.ok ? (h.smtp.mode === "dry-run" ? "warn" : "ok") : "bad");
    })
    .catch(function () {
      document.getElementById("badges").innerHTML = '<span class="badge bad">健康检查失败</span>';
    });

  /* ================= 对话 ================= */
  var messagesBox = document.getElementById("messages");
  var chatForm = document.getElementById("chat-form");
  var chatText = document.getElementById("chat-text");
  var chatSend = document.getElementById("chat-send");
  var chatHint = document.getElementById("chat-hint");

  function scrollToBottom() {
    messagesBox.scrollTop = messagesBox.scrollHeight;
  }

  function addMessage(role, text, meta) {
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;

    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);

    if (meta && meta.tool_trace && meta.tool_trace.length) {
      var trace = document.createElement("div");
      trace.className = "trace";
      meta.tool_trace.forEach(function (t) {
        var chip = document.createElement("span");
        chip.className = "chip " + (t.ok ? "ok" : "bad");
        chip.textContent = t.name + (t.ok ? " ✓" : " ✗ " + (t.error || ""));
        trace.appendChild(chip);
      });
      wrap.appendChild(trace);
    }

    if (meta && meta.references && meta.references.length) {
      var refs = document.createElement("div");
      refs.className = "refs";
      refs.appendChild(document.createTextNode("依据："));
      meta.references.forEach(function (r, index) {
        if (index) refs.appendChild(document.createTextNode("、"));
        if (r.url) {
          var link = document.createElement("a");
          link.className = "ref-link";
          link.href = r.url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = r.title || r.url;
          refs.appendChild(link);
        } else {
          var span = document.createElement("span");
          span.textContent = r.chunk_id
            ? "PDF 第 " + r.page + " 页 · " + r.chunk_id
            : (r.title || JSON.stringify(r));
          refs.appendChild(span);
        }
      });
      wrap.appendChild(refs);
    }

    if (meta && meta.artifacts && meta.artifacts.length) {
      var arts = document.createElement("div");
      arts.className = "refs";
      meta.artifacts.forEach(function (a) {
        var link = document.createElement("a");
        link.className = "artifact";
        link.href = a.url;
        link.setAttribute("download", "");
        link.textContent = "下载 PPT：" + (a.title || "演示文稿") +
          (a.pages ? "（" + a.pages + " 页）" : "");
        arts.appendChild(link);
      });
      wrap.appendChild(arts);
    }

    if (meta && meta.truncated) {
      var note = document.createElement("div");
      note.className = "refs";
      note.textContent = "（历史已按窗口截断）";
      wrap.appendChild(note);
    }

    messagesBox.appendChild(wrap);
    scrollToBottom();
  }

  function setChatBusy(busy) {
    chatSend.disabled = busy;
    chatSend.textContent = busy ? "…" : "发送";
    chatText.disabled = busy;
  }

  function restoreHistory() {
    fetch("/api/sessions/" + encodeURIComponent(sessionId) + "/messages")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var list = (data.messages || []).slice(-20);
        if (!list.length) return;
        messagesBox.innerHTML = "";
        list.forEach(function (m) {
          addMessage(m.role, m.content, { references: m.refs || [] });
        });
      })
      .catch(function () { /* 历史恢复失败不影响对话 */ });
  }

  function sendMessage() {
    var text = chatText.value.trim();
    if (!text) return;
    chatText.value = "";
    addMessage("user", text, null);
    setChatBusy(true);
    chatHint.textContent = "正在思考…";

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text: text })
    })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, status: res.status, body: body }; });
      })
      .then(function (result) {
        if (!result.ok) {
          addMessage("assistant", "出错了（" + result.status + "）：" + (result.body.error || "未知错误"), null);
          return;
        }
        addMessage("assistant", result.body.reply, result.body);
        chatHint.textContent = "上下文保留最近 12 条 / 6000 字；工具调用显示在气泡下方";
      })
      .catch(function (err) {
        addMessage("assistant", "请求失败：" + err.message, null);
      })
      .finally(function () {
        setChatBusy(false);
        chatText.focus();
      });
  }

  chatForm.addEventListener("submit", function (event) {
    event.preventDefault();
    sendMessage();
  });

  chatText.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!chatSend.disabled) sendMessage();
    }
  });

  restoreHistory();

  /* ================= 长期记忆面板 ================= */
  var memoryList = document.getElementById("memory-list");

  function loadMemory() {
    memoryList.innerHTML = '<p class="muted">加载中…</p>';
    fetch("/api/sessions/" + encodeURIComponent(sessionId) + "/memory")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var entities = data.entities || [];
        if (!entities.length) {
          memoryList.innerHTML = '<p class="muted">记忆为空。上传一份会议 TXT 或对我说「我叫…」就会写入。</p>';
          return;
        }
        var groups = { person: "人物", time: "时间", place: "地点", topic: "主题" };
        var html = "";
        Object.keys(groups).forEach(function (type) {
          var items = entities.filter(function (e) { return e.type === type; });
          if (!items.length) return;
          html += '<div class="mem-group"><div class="mem-title">' + groups[type] +
            ' <span class="count">' + items.length + "</span></div>";
          items.slice(0, 6).forEach(function (e) {
            var from = (e.sources || []).some(function (s) { return s.indexOf("message:") === 0; })
              ? "对话" : "会议";
            var self = (e.attrs && e.attrs.self) ? ' <span class="tag low">本人</span>' : "";
            html += '<div class="mem-item"><span class="mem-name">' + esc(e.name) + "</span>" +
              self + '<span class="chip">' + from + "</span></div>";
          });
          html += "</div>";
        });
        memoryList.innerHTML = html;
      })
      .catch(function () {
        memoryList.innerHTML = '<p class="muted">记忆加载失败</p>';
      });
  }

  document.getElementById("mem-refresh").addEventListener("click", loadMemory);
  loadMemory();

  /* ================= PDF 知识库上传 ================= */
  var pdfForm = document.getElementById("pdf-form");
  var pdfBtn = document.getElementById("pdf-submit-btn");
  var pdfStatus = document.getElementById("pdf-status");

  function setPdfStatus(message, isError) {
    pdfStatus.textContent = message;
    pdfStatus.classList.remove("hidden");
    pdfStatus.classList.toggle("error", !!isError);
  }

  pdfForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var input = document.getElementById("pdf-file");
    if (!input.files.length) {
      setPdfStatus("请选择 PDF 文件", true);
      return;
    }
    var data = new FormData();
    data.append("file", input.files[0]);
    data.append("session_id", sessionId);

    pdfBtn.disabled = true;
    pdfBtn.textContent = "建索引中…";
    setPdfStatus("正在解析 PDF、切块并建索引，请稍候", false);

    fetch("/api/upload/pdf", { method: "POST", body: data })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, status: res.status, body: body }; });
      })
      .then(function (result) {
        if (!result.ok) {
          setPdfStatus("处理失败（" + result.status + "）：" + (result.body.error || "未知错误"), true);
          return;
        }
        var b = result.body;
        setPdfStatus(
          (b.status === "reused" ? "已存在，复用索引：" : "完成：") +
          b.pages + " 页 / " + b.chunks + " 块 · doc_id=" + b.doc_id,
          false
        );
        addMessage("assistant", "PDF 已进入全局知识库（" + b.pages + " 页 / " + b.chunks +
          " 块，doc_id=" + b.doc_id + "）。可以直接问文档里的内容，我会标出页码与 chunk_id。", null);
      })
      .catch(function (err) {
        setPdfStatus("请求失败：" + err.message, true);
      })
      .finally(function () {
        pdfBtn.disabled = false;
        pdfBtn.textContent = "上传并建索引";
      });
  });

  /* ================= PPT 生成 ================= */
  var pptForm = document.getElementById("ppt-form");
  var pptBtn = document.getElementById("ppt-submit-btn");
  var pptStatus = document.getElementById("ppt-status");
  var pptResult = document.getElementById("ppt-result");

  function setPptStatus(message, isError) {
    pptStatus.textContent = message;
    pptStatus.classList.remove("hidden");
    pptStatus.classList.toggle("error", !!isError);
  }

  function showPptLink(container, artifact) {
    container.innerHTML = "";
    var link = document.createElement("a");
    link.className = "artifact";
    link.href = artifact.url;
    link.setAttribute("download", "");
    link.textContent = "下载 PPT：" + (artifact.title || "演示文稿") +
      (artifact.pages ? "（" + artifact.pages + " 页）" : "");
    container.appendChild(link);
    container.classList.remove("hidden");
  }

  pptForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var topic = document.getElementById("ppt-topic").value.trim();
    if (!topic) {
      setPptStatus("请填写 PPT 主题", true);
      return;
    }
    pptBtn.disabled = true;
    pptBtn.textContent = "生成中…";
    setPptStatus("正在生成大纲并渲染 .pptx，请稍候", false);
    pptResult.classList.add("hidden");

    fetch("/api/ppt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, spec: topic })
    })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, status: res.status, body: body }; });
      })
      .then(function (result) {
        if (!result.ok) {
          setPptStatus("生成失败（" + result.status + "）：" + (result.body.error || "未知错误"), true);
          return;
        }
        var b = result.body;
        setPptStatus("完成：" + b.title + " · " + b.pages + " 页" +
          (b.outline_source === "fallback" ? "（模型不可用，已用通用大纲）" : ""), false);
        var artifact = { type: "ppt", title: b.title, url: b.download_url, pages: b.pages };
        showPptLink(pptResult, artifact);
        addMessage("assistant", "PPT 已生成：" + b.title + "（" + b.pages + " 页）。" +
          (b.note ? "\n备注：" + b.note : ""), { artifacts: [artifact] });
      })
      .catch(function (err) {
        setPptStatus("请求失败：" + err.message, true);
      })
      .finally(function () {
        pptBtn.disabled = false;
        pptBtn.textContent = "生成并下载";
      });
  });

  /* ================= 会议上传 ================= */
  var form = document.getElementById("upload-form");
  var btn = document.getElementById("submit-btn");
  var statusBox = document.getElementById("status");
  var report = document.getElementById("report");
  var emptyState = document.getElementById("empty-state");

  function setStatus(message, isError) {
    statusBox.textContent = message;
    statusBox.classList.remove("hidden");
    statusBox.classList.toggle("error", !!isError);
  }

  function setBusy(busy, label) {
    btn.disabled = busy;
    btn.textContent = busy ? label || "处理中…" : "上传并分析";
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var fileInput = document.getElementById("file");
    if (!fileInput.files.length) {
      setStatus("请选择会议 TXT 文件", true);
      return;
    }
    var data = new FormData();
    data.append("file", fileInput.files[0]);
    data.append("mail_to", document.getElementById("mail_to").value.trim());
    data.append("session_id", sessionId);

    setBusy(true, "分析中…");
    setStatus("正在上传并分析，请稍候", false);

    fetch("/api/upload/meeting", { method: "POST", body: data })
      .then(function (res) {
        return res.json().then(function (body) { return { ok: res.ok, status: res.status, body: body }; });
      })
      .then(function (result) {
        if (!result.ok) {
          setStatus("处理失败（" + result.status + "）：" + (result.body.error || "未知错误"), true);
          return;
        }
        setStatus("完成：" + result.body.risks.length + " 条风险，" + result.body.todos.length + " 条待办", false);
        render(result.body);
        addMessage("assistant", "会议《" + (result.body.meeting.topic || "未命名") + "》分析完成："
          + result.body.risks.length + " 条风险、" + result.body.todos.length + " 条待办，发信状态 "
          + result.body.mail_status + "。", null);
      })
      .catch(function (err) {
        setStatus("请求失败：" + err.message, true);
      })
      .finally(function () {
        setBusy(false);
      });
  });

  function mailBadge(status) {
    var text = {
      sent: "已发送",
      sending: "发送中",
      dry_run: "未真发（dry-run 落 .eml）",
      skipped_duplicate: "已发送过，跳过",
      not_needed: "无需发信",
      failed: "发送失败"
    }[status] || status;
    var cls = { sent: "ok", sending: "warn", dry_run: "warn", skipped_duplicate: "ok", not_needed: "low", failed: "bad" }[status] || "";
    return '<span class="badge ' + cls + '">' + esc(text) + "</span>";
  }

  function render(data) {
    emptyState.classList.add("hidden");
    report.classList.remove("hidden");

    document.getElementById("mail-badge").innerHTML = mailBadge(data.mail_status);

    var m = data.meeting || {};
    document.getElementById("meeting-meta").innerHTML =
      "主题：<b>" + esc(m.topic || "-") + "</b><br>时间：" + esc(m.held_at || "-") +
      " ｜ 地点：" + esc(m.place || "-") +
      " ｜ 参会：" + esc((m.attendees || []).join("、") || "-") +
      "<br>meeting_id：<code>" + esc(data.meeting_id) + "</code>" +
      (data.reused ? ' ｜ <span class="badge low">重复上传，复用已有结论</span>' : "");

    var g = data.gates || {};
    document.getElementById("gates-summary").innerHTML =
      "闸门：模型给出 <b>" + (g.kept + g.dropped) + "</b> 条 → 证据校验丢弃 <b>" + g.dropped +
      "</b> 条 → 保留 <b>" + g.kept + "</b> 条" +
      (g.notes && g.notes.length ? "<br>" + g.notes.map(esc).join("；") : "");

    document.querySelector("#risk-table tbody").innerHTML = (data.risks || []).map(function (r) {
      return "<tr><td><span class='tag " + esc(r.severity) + "'>" + esc(r.severity) + "</span></td>" +
        "<td>" + esc(r.basis) + "</td>" +
        "<td class='quote'>" + esc(r.evidence_quote) + "</td>" +
        "<td>" + esc(r.suggestion || "-") + "</td></tr>";
    }).join("");
    document.getElementById("risk-count").textContent = (data.risks || []).length;

    document.querySelector("#todo-table tbody").innerHTML = (data.todos || []).map(function (t) {
      return "<tr><td>" + esc(t.owner || "未指定") + "</td>" +
        "<td>" + esc(t.action) + "</td>" +
        "<td>" + esc(t.due || "未定") + "</td>" +
        "<td>" + esc(t.status) + "</td></tr>";
    }).join("");
    document.getElementById("todo-count").textContent = (data.todos || []).length;

    document.querySelector("#send-table tbody").innerHTML = (data.send_logs || []).map(function (s) {
      return "<tr><td>" + esc(s.ts) + "</td>" +
        "<td>" + esc(s.status) + "</td>" +
        "<td>" + esc((s.recipients || []).join(", ")) + "</td>" +
        "<td><code>" + esc(s.dedupe_key || "—") + "</code></td>" +
        "<td>" + esc(s.error || "") + "</td></tr>";
    }).join("");
    document.getElementById("send-detail").textContent = data.mail_detail || "";
  }
})();
