"use strict";

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function parseMarkdown(md) {
  if (!md) return "";
  const source = String(md)
    .replace(/\r\n?/g, "\n")
    // Genie Agent citation: \[[1](https://... )\] -> [1](https://...)
    .replace(/\\\[\[(\d+)\]\((https?:\/\/[^)\s]+)\)\\\]/g, "[$1]($2)")
    .replace(/\[\[(\d+)\]\]\((https?:\/\/[^)\s]+)\)/g, "[$1]($2)");

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : null;
    } catch (_) {
      return null;
    }
  }

  function renderInline(value) {
    const tokens = [];
    let text = value.replace(/`([^`\n]+)`/g, (_, code) => {
      const index = tokens.push(`<code class="inline-code">${escapeHtml(code)}</code>`) - 1;
      return `\u0000${index}\u0000`;
    });
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, (match, label, href) => {
      const url = safeUrl(href);
      if (!url) return match;
      const visibleLabel = /^\d+$/.test(label.trim()) ? `[${label.trim()}]` : label;
      const index = tokens.push(
        `<a class="citation-link" href="${url}" target="_blank" rel="noopener noreferrer">${escapeHtml(visibleLabel)}</a>`
      ) - 1;
      return `\u0000${index}\u0000`;
    });
    text = escapeHtml(text)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
      .replace(/_([^_\n]+)_/g, "<em>$1</em>");
    return text.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)] || "");
  }

  function tableCells(line) {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((cell) => cell.trim());
  }

  function isTableDivider(line) {
    return tableCells(line).every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  const lines = source.split("\n");
  const blocks = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    if (line.trim().startsWith("```")) {
      const language = line.trim().slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push(`<pre class="code-block"><code${language ? ` data-language="${escapeHtml(language)}"` : ""}>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      blocks.push(`<h${level} class="md-heading">${renderInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = tableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(tableCells(lines[index++]));
      blocks.push(`<div class="table-wrap"><table class="md-table"><thead><tr>${headers.map((cell) => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }

    const listMatch = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const tag = ordered ? "ol" : "ul";
      const items = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
        if (!match || Boolean(match[2]) !== ordered) break;
        items.push(`<li>${renderInline(match[3])}</li>`);
        index += 1;
      }
      blocks.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim()
      && !/^\s*(#{1,6})\s+/.test(lines[index])
      && !/^\s*(?:[-*+]|\d+\.)\s+/.test(lines[index])
      && !lines[index].trim().startsWith("```")) {
      if (lines[index].includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) break;
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
  }
  return blocks.join("");
}

const STATUS_TEXT = {
  SUBMITTED: "질문을 전달하고 있습니다",
  FETCHING_METADATA: "관련 데이터를 확인하고 있습니다",
  ASKING_AI: "답변을 작성하고 있습니다",
  EXECUTING_QUERY: "데이터를 조회하고 있습니다",
  COMPLETED: "완료",
  FAILED: "처리 실패",
};

const TERMINAL_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"]);
const POLL_INTERVAL_MS = 1500;
const MAX_POLL_ATTEMPTS = 180;

const state = {
  mode: "CHAT",
  agents: [],
  selectedAgentId: null,
  activeConversationId: null,
  activeConversationMode: null,
  conversations: [],
  pollingController: null,
  busy: false,
  userScrolledUp: false,
};

const elements = {
  sidebar: document.querySelector("#sidebar"),
  collapseSidebarButton: document.querySelector("#collapseSidebarButton"),
  openSidebarButton: document.querySelector("#openSidebarButton"),
  sidebarBackdrop: document.querySelector("#sidebarBackdrop"),
  newChatButton: document.querySelector("#newChatButton"),
  headerNewChatButton: document.querySelector("#headerNewChatButton"),
  historySearchInput: document.querySelector("#historySearchInput"),
  refreshHistoryButton: document.querySelector("#refreshHistoryButton"),
  historyList: document.querySelector("#historyList"),
  agentSelect: document.querySelector("#agentSelect"),
  agentName: document.querySelector("#agentName"),
  copyConversationButton: document.querySelector("#copyConversationButton"),
  emptyState: document.querySelector("#emptyState"),
  messageList: document.querySelector("#messageList"),
  composerForm: document.querySelector("#composerForm"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  scrollToBottomButton: document.querySelector("#scrollToBottomButton"),
  conversationLabel: document.querySelector("#conversationLabel"),
  toast: document.querySelector("#toast"),
  historyItemTemplate: document.querySelector("#historyItemTemplate"),
  userMessageTemplate: document.querySelector("#userMessageTemplate"),
  assistantMessageTemplate: document.querySelector("#assistantMessageTemplate"),
  modeButtons: document.querySelectorAll(".mode-switch button"),
};

function selectedEndpointAgentId() {
  const selected = state.agents.find((agent) => agent.agent_id === state.selectedAgentId);
  if (!selected) return state.selectedAgentId;
  return state.mode === "AGENT" ? selected.agent_mode_id : selected.chat_agent_id;
}

function endpointAgentIdForMode(mode) {
  const selected = state.agents.find((agent) => agent.agent_id === state.selectedAgentId);
  if (!selected) return state.selectedAgentId;
  return mode === "AGENT" ? selected.agent_mode_id : selected.chat_agent_id;
}

function agentModeLabel(agent) {
  if (agent?.chat_agent_id && agent?.agent_mode_id) return "Chat · Agent";
  if (agent?.agent_mode_id) return "Agent";
  return "Chat";
}

function syncLocationState() {
  const url = new URL(window.location.href);
  if (state.selectedAgentId) url.searchParams.set("agent", state.selectedAgentId);
  else url.searchParams.delete("agent");
  url.searchParams.set("mode", state.mode);
  if (state.activeConversationId) url.searchParams.set("conversation", state.activeConversationId);
  else url.searchParams.delete("conversation");
  window.history.replaceState(null, "", url);
}

function ensureSupportedMode(agent) {
  if (!agent) return;
  if (state.mode === "AGENT" && !agent.agent_mode_id) state.mode = "CHAT";
  if (state.mode === "CHAT" && !agent.chat_agent_id) state.mode = "AGENT";
  elements.modeButtons.forEach((button) => {
    const supported = button.dataset.mode === "AGENT" ? Boolean(agent.agent_mode_id) : Boolean(agent.chat_agent_id);
    button.disabled = !supported;
    button.classList.toggle("active", supported && button.dataset.mode === state.mode);
  });
}

function showToast(message, duration = 3000) {
  if (!elements.toast) return;
  elements.toast.textContent = message;
  elements.toast.classList.add("show");
  setTimeout(() => {
    elements.toast.classList.remove("show");
  }, duration);
}

async function apiFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const text = await response.text();
  let payload = {};
  if (text) {
    try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
  }
  if (!response.ok) {
    const method = (options.method || "GET").toUpperCase();
    const traceId = response.headers.get("x-request-id");
    const errorCode = payload.error_code || payload.code;
    const reason = payload.detail || payload.message || "응답 본문 없음";
    const diagnostics = [
      `${method} ${path}`,
      `HTTP ${response.status}`,
      errorCode,
      traceId ? `trace=${traceId}` : null,
    ].filter(Boolean).join(" · ");
    const error = new Error(`${reason} (${diagnostics})`);
    error.status = response.status;
    error.payload = payload;
    error.path = path;
    error.traceId = traceId;
    throw error;
  }
  return payload;
}

// Bootstrap (Agent 목록)
async function bootstrap() {
  try {
    const data = await apiFetch("/custom/chat/api/bootstrap");
    state.agents = data.agents || [];
    const url = new URL(window.location.href);
    const requestedAgent = url.searchParams.get("agent");
    const requestedMode = url.searchParams.get("mode")?.toUpperCase();
    const linkedAgent = state.agents.find((agent) =>
      [agent.agent_id, agent.chat_agent_id, agent.agent_mode_id].includes(requestedAgent)
    );
    state.selectedAgentId = linkedAgent?.agent_id || data.default_agent_id || (state.agents[0] && state.agents[0].agent_id);
    if (requestedMode === "AGENT" && linkedAgent?.agent_mode_id) state.mode = "AGENT";
    if (requestedMode === "CHAT" && linkedAgent?.chat_agent_id) state.mode = "CHAT";
    ensureSupportedMode(linkedAgent || state.agents.find((agent) => agent.agent_id === state.selectedAgentId));
    renderAgentSelect();
    if (state.selectedAgentId) {
      await loadConversations();
      const requestedConversation = url.searchParams.get("conversation");
      if (requestedConversation && state.conversations.some((item) => item.conversation_id === requestedConversation)) {
        const conversation = state.conversations.find((item) => item.conversation_id === requestedConversation);
        await selectConversation(conversation);
      }
    }
  } catch (err) {
    showToast(`초기화 실패: ${err.message}`);
  }
}

function renderAgentSelect() {
  if (!elements.agentSelect) return;
  elements.agentSelect.innerHTML = "";
  state.agents.forEach((agent) => {
    const option = document.createElement("option");
    option.value = agent.agent_id;
    option.textContent = `${agent.agent_name} · ${agentModeLabel(agent)}`;
    if (agent.agent_id === state.selectedAgentId) option.selected = true;
    elements.agentSelect.appendChild(option);
  });
  const current = state.agents.find((a) => a.agent_id === state.selectedAgentId);
  elements.agentSelect.disabled = state.agents.length <= 1;
  if (current && elements.agentName) {
    elements.agentName.textContent = current.agent_name;
  }
  syncLocationState();
}

// 대화 이력 목록 로드
async function loadConversations() {
  if (!state.selectedAgentId) return;
  try {
    const selected = state.agents.find((agent) => agent.agent_id === state.selectedAgentId);
    const endpointAgentId = selectedEndpointAgentId() || selected?.chat_agent_id || selected?.agent_mode_id;
    const routeMode = endpointAgentId === selected?.agent_mode_id ? "AGENT" : "CHAT";
    if (!endpointAgentId) {
      state.conversations = [];
      renderHistoryList();
      return;
    }
    const data = await apiFetch(`/custom/chat/api/agents/${encodeURIComponent(endpointAgentId)}/conversations?mode=${routeMode}`);
    state.conversations = data.conversations || [];
    renderHistoryList();
  } catch (err) {
    console.error("대화 이력 로드 실패:", err);
    showToast(`대화 이력 로드 실패: ${err.message}`, 7000);
  }
}

function renderHistoryList() {
  if (!elements.historyList) return;
  const searchTerm = (elements.historySearchInput ? elements.historySearchInput.value : "").trim().toLowerCase();
  elements.historyList.innerHTML = "";

  const filtered = state.conversations.filter((c) => {
    const title = (c.title || "새 대화").toLowerCase();
    return title.includes(searchTerm);
  });

  filtered.forEach((conv) => {
    const fragment = elements.historyItemTemplate.content.cloneNode(true);
    const item = fragment.querySelector(".history-item");
    const titleBtn = fragment.querySelector(".history-title");
    const menuDetails = fragment.querySelector(".history-menu");

    titleBtn.textContent = `${conv.conversation_mode === "AGENT" ? "✦" : "💬"} ${conv.title || "새 대화"}`;
    if (conv.conversation_id === state.activeConversationId) {
      item.classList.add("active");
    }

    titleBtn.addEventListener("click", () => {
      selectConversation(conv);
    });

    menuDetails.querySelectorAll("button[data-action]").forEach((btn) => {
      if (conv.conversation_mode === "AGENT" && btn.dataset.action === "delete") btn.hidden = true;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        menuDetails.removeAttribute("open");
        const action = btn.dataset.action;
        if (action === "rename") handleRenameConversation(conv);
        else if (action === "copy") handleCopyId(conv.conversation_id);
        else if (action === "delete") handleDeleteConversation(conv);
      });
    });

    elements.historyList.appendChild(fragment);
  });
}

async function selectConversation(conversation) {
  if (state.busy) return;
  const conversationId = conversation.conversation_id;
  const conversationMode = conversation.conversation_mode || "CHAT";
  stopPolling();
  state.mode = conversationMode;
  state.activeConversationId = conversationId;
  state.activeConversationMode = conversationMode;
  elements.modeButtons.forEach((item) => item.classList.toggle("active", item.dataset.mode === state.mode));
  renderHistoryList();
  updateHeaderAndMeta();

  if (elements.emptyState) elements.emptyState.hidden = true;
  if (elements.messageList) elements.messageList.innerHTML = "";

  try {
    if (state.mode === "AGENT") {
      await window.AgentMode.loadConversation({
        agentId: selectedEndpointAgentId(), conversationId, elements,
        addUserMessageUI, addAssistantMessageUI, parseMarkdown, showToast, scrollToBottom,
      });
    } else {
      const data = await apiFetch(
        `/custom/chat/api/agents/${encodeURIComponent(selectedEndpointAgentId())}/conversations/${encodeURIComponent(conversationId)}/messages`
      );
      renderExistingMessages(data.messages || []);
    }
  } catch (err) {
    showToast(`대화 메시지 로드 실패: ${err.message}`);
  }
}

function renderExistingMessages(messages) {
  if (!elements.messageList) return;
  elements.messageList.innerHTML = "";
  if (!messages.length) {
    if (elements.emptyState) elements.emptyState.hidden = false;
    return;
  }
  if (elements.emptyState) elements.emptyState.hidden = true;

  messages.forEach((msg) => {
    const userQuery = msg.content || msg.msg_cn || msg.query || "";
    if (userQuery) {
      addUserMessageUI(userQuery);
    }
    const row = addAssistantMessageUI();
    updateAssistantMessageUI(row, msg);
  });
  scrollToBottom();
}

function startNewChat() {
  stopPolling();
  state.activeConversationId = null;
  state.activeConversationMode = null;
  state.busy = false;
  if (elements.messageList) elements.messageList.innerHTML = "";
  if (elements.emptyState) elements.emptyState.hidden = false;
  renderHistoryList();
  updateHeaderAndMeta();
  if (elements.messageInput) elements.messageInput.focus();
}

function updateHeaderAndMeta() {
  if (state.activeConversationId) {
    if (elements.copyConversationButton) elements.copyConversationButton.hidden = false;
    if (elements.conversationLabel) {
      elements.conversationLabel.textContent = `Conversation · ${state.activeConversationId.substring(0, 8)}...`;
    }
  } else {
    if (elements.copyConversationButton) elements.copyConversationButton.hidden = true;
    if (elements.conversationLabel) elements.conversationLabel.textContent = "새 Conversation";
  }
  syncLocationState();
}

function stopPolling() {
  if (state.pollingController) {
    state.pollingController.abort();
    state.pollingController = null;
  }
}

// UI 헬퍼
function addUserMessageUI(content) {
  if (elements.emptyState) elements.emptyState.hidden = true;
  const fragment = elements.userMessageTemplate.content.cloneNode(true);
  fragment.querySelector(".user-bubble").textContent = content;
  elements.messageList.appendChild(fragment);
  scrollToBottom();
}

function addAssistantMessageUI() {
  if (elements.emptyState) elements.emptyState.hidden = true;
  const fragment = elements.assistantMessageTemplate.content.cloneNode(true);
  const row = fragment.querySelector(".assistant-row");
  elements.messageList.appendChild(fragment);
  scrollToBottom();
  return row;
}

function updateAssistantMessageUI(row, payload) {
  const msg = payload.message || payload;
  const rawStatus = msg.status || msg.msg_stts_cd || "SUBMITTED";
  const displayStatus = STATUS_TEXT[rawStatus] || rawStatus;

  const statusSpan = row.querySelector(".message-status");
  const answerDiv = row.querySelector(".answer-content");
  const attachmentList = row.querySelector(".attachment-list");
  const followUpSection = row.querySelector(".follow-up-section");
  const followUpList = row.querySelector(".follow-up-list");
  const techDetails = row.querySelector(".technical-details dl");

  if (statusSpan) statusSpan.textContent = displayStatus;

  // 기존 open 상태 기억 (Polling 깜빡임 방지)
  const openSummaries = new Set();
  if (attachmentList) {
    attachmentList.querySelectorAll("details[open] summary").forEach((sum) => {
      openSummaries.add(sum.textContent.trim());
    });
    attachmentList.innerHTML = "";
  }
  if (followUpList) followUpList.innerHTML = "";

  // 상세 정보 세팅
  if (techDetails) {
    const traceId = payload.trace_id || "-";
    const convId = state.activeConversationId || "-";
    const msgId = msg.message_id || msg.id || "-";
    techDetails.innerHTML = `
      <dt>Status</dt><dd>${rawStatus}</dd>
      <dt>Message ID</dt><dd>${msgId}</dd>
      <dt>Conversation ID</dt><dd>${convId}</dd>
    `;
  }

  if (msg.error || payload.error) {
    const errObj = msg.error || payload.error;
    const errText = typeof errObj === "string" ? errObj : (errObj.message || errObj.error || "오류가 발생했습니다.");
    answerDiv.className = "answer-content error-card";
    answerDiv.textContent = errText;
    return;
  }

  const attachments = Array.isArray(msg.attachments) ? msg.attachments : [];
  const textAnswers = [];
  let queryDescription = "";

  attachments.forEach((att) => {
    // Databricks 공식 응답은 시각화 객체를 `viz`로 제공한다.
    // 이전 응답 형식과의 호환성을 위해 `visualization`도 fallback으로 허용한다.
    const visualization = att.viz || att.visualization;
    // 텍스트 답변 수집 (중복 제거)
    if (att.text && att.text.content) {
      const trimmed = att.text.content.trim();
      if (trimmed && !textAnswers.includes(trimmed)) {
        textAnswers.push(trimmed);
      }
    }
    // SQL Query 수집
    if (att.query) {
      const title = att.query.title || "생성 SQL";
      const sqlText = att.query.query || att.query.sql || "";
      if (att.query.description) {
        queryDescription = att.query.description.trim();
      }
      attachmentList.appendChild(createDetailsCard(title, sqlText, "sql"));
    }
    // 시각화 Attachment를 Gateway 이미지 API를 통해 화면에 표시
    if (visualization) {
      const title = visualization.title || "시각화 정보";
      const attachmentId = att.attachment_id || att.id || visualization.attachment_id;
      const messageId = msg.message_id || msg.id;
      if (attachmentId && messageId && state.activeConversationId && state.selectedAgentId) {
        attachmentList.appendChild(createVisualizationCard(title, messageId, attachmentId));
      } else {
        attachmentList.appendChild(createDetailsCard(title, JSON.stringify(visualization, null, 2)));
      }
    }
    // 추천 질문 버튼 수집
    if (att.suggested_questions && Array.isArray(att.suggested_questions.questions)) {
      if (followUpSection) followUpSection.hidden = false;
      att.suggested_questions.questions.forEach((q) => {
        const btn = document.createElement("button");
        btn.className = "suggestion-chip";
        btn.type = "button";
        btn.textContent = q;
        btn.addEventListener("click", () => {
          appendInputText(q);
        });
        if (followUpList) followUpList.appendChild(btn);
      });
    }
  });

  // attachment에 text가 없고 msg.content만 존재하는 경우 fallback (최종 완료시에만 유효)
  if (textAnswers.length === 0 && TERMINAL_STATUSES.has(rawStatus) && (msg.content || msg.msg_cn)) {
    const rawContent = (msg.content || msg.msg_cn || "").trim();
    if (rawContent && !textAnswers.includes(rawContent)) {
      textAnswers.push(rawContent);
    }
  }

  // 기존 open 상태 복원 (깜빡임 완화)
  if (attachmentList) {
    attachmentList.querySelectorAll("details").forEach((d) => {
      const sum = d.querySelector("summary");
      if (sum && openSummaries.has(sum.textContent.trim())) {
        d.open = true;
      }
    });
  }

  if (textAnswers.length > 0) {
    answerDiv.className = "answer-content";
    answerDiv.innerHTML = parseMarkdown(textAnswers.join("\n\n"));
  } else if (queryDescription) {
    answerDiv.className = "answer-content";
    answerDiv.innerHTML = parseMarkdown(queryDescription);
  } else if (TERMINAL_STATUSES.has(rawStatus)) {
    answerDiv.className = "answer-content";
    answerDiv.textContent = rawStatus === "COMPLETED" ? "응답 조회가 완료되었습니다." : `처리가 ${displayStatus} 상태로 종료되었습니다.`;
  } else {
    answerDiv.className = "answer-content pending typing-dots";
    answerDiv.textContent = STATUS_TEXT[rawStatus] || "답변을 준비하고 있습니다";
  }

  scrollToBottom();
}

function createDetailsCard(title, content, type = "text") {
  const details = document.createElement("details");
  details.className = "attachment-card";
  const summary = document.createElement("summary");
  summary.textContent = title;
  const pre = document.createElement("pre");
  if (type === "sql") {
    pre.className = "sql-code";
  }
  pre.textContent = content || "내용 없음";
  details.appendChild(summary);
  details.appendChild(pre);
  return details;
}

function createVisualizationCard(title, messageId, attachmentId) {
  const figure = document.createElement("figure");
  figure.className = "visualization-card";

  const caption = document.createElement("figcaption");
  caption.textContent = title;

  const image = document.createElement("img");
  image.className = "visualization-image";
  image.alt = `${title} 시각화`;
  image.loading = "eager";
  image.decoding = "async";
  const imageUrl = `/custom/chat/api/agents/${encodeURIComponent(state.selectedAgentId)}`
    + `/conversations/${encodeURIComponent(state.activeConversationId)}`
    + `/messages/${encodeURIComponent(messageId)}`
    + `/attachments/${encodeURIComponent(attachmentId)}/visualization`;
  image.addEventListener("load", () => {
    const objectUrl = image.dataset.objectUrl;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      delete image.dataset.objectUrl;
    }
    scrollToBottom();
  });

  figure.appendChild(caption);
  figure.appendChild(image);
  void loadVisualizationWithRetries(figure, image, imageUrl);
  return figure;
}

async function loadVisualizationWithRetries(figure, image, imageUrl) {
  // Message가 COMPLETED여도 visualization PNG 생성은 수 초 늦게 완료될 수 있다.
  // 동일 URL을 no-store로 다시 조회하여 초기 안내 PNG를 최종 차트로 교체한다.
  const retryDelays = [0, 3000, 5000, 7000];
  let loaded = false;

  for (const delay of retryDelays) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    if (!figure.isConnected) return;
    try {
      const response = await fetch(imageUrl, {
        cache: "no-store",
        headers: { Accept: "image/png,application/octet-stream" },
      });
      if (!response.ok) {
        throw new Error(`시각화 요청 실패 (${response.status})`);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const previousUrl = image.dataset.objectUrl;
      image.dataset.objectUrl = objectUrl;
      image.src = objectUrl;
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      loaded = true;
    } catch (err) {
      console.warn("시각화 이미지 조회 실패:", err);
    }
  }

  if (!loaded && figure.isConnected) {
    const error = document.createElement("div");
    error.className = "visualization-error";
    error.textContent = "시각화 이미지를 불러오지 못했습니다.";
    image.replaceWith(error);
  }
}

function appendInputText(text) {
  if (!elements.messageInput) return;
  const current = elements.messageInput.value;
  if (current.trim()) {
    elements.messageInput.value = `${current} ${text}`;
  } else {
    elements.messageInput.value = text;
  }
  resizeComposer();
  elements.messageInput.focus();
}

// 메시지 전송 및 Polling
async function sendMessage(content) {
  if (state.busy || !selectedEndpointAgentId()) return;
  state.busy = true;
  if (elements.sendButton) elements.sendButton.disabled = true;

  state.userScrolledUp = false;
  addUserMessageUI(content);
  const assistantRow = addAssistantMessageUI();
  scrollToBottom(true);

  try {
    if (state.mode === "AGENT") {
      const result = await window.AgentMode.send({
        agentId: selectedEndpointAgentId(), conversationId: state.activeConversationId,
        content, row: assistantRow, parseMarkdown, showToast, scrollToBottom,
        onConversationCreated: async (conversationId) => {
          state.activeConversationId = conversationId;
          state.activeConversationMode = "AGENT";
          updateHeaderAndMeta();
          await loadConversations();
        },
      });
      if (result.conversationId) state.activeConversationId = result.conversationId;
      state.activeConversationMode = "AGENT";
      await loadConversations();
      updateHeaderAndMeta();
      return;
    }
    const isNew = !state.activeConversationId;
    const url = isNew
      ? `/custom/chat/api/agents/${encodeURIComponent(selectedEndpointAgentId())}/start-conversation`
      : `/custom/chat/api/agents/${encodeURIComponent(selectedEndpointAgentId())}/conversations/${encodeURIComponent(state.activeConversationId)}/messages`;

    const payload = await apiFetch(url, {
      method: "POST",
      body: JSON.stringify({
        content,
        enable_visualization: true,
      }),
    });

    const msgObj = payload.message || payload;
    const convId = payload.conversation_id || (payload.conversation && payload.conversation.id) || msgObj.conversation_id;
    const messageId = payload.message_id || msgObj.message_id || msgObj.id;

    if (isNew && convId) {
      state.activeConversationId = convId;
      await loadConversations();
      updateHeaderAndMeta();
    }

    updateAssistantMessageUI(assistantRow, payload);

    const status = msgObj.status || "SUBMITTED";
    if (!TERMINAL_STATUSES.has(status) && messageId && state.activeConversationId) {
      await pollMessageStatus(state.activeConversationId, messageId, assistantRow);
    }
  } catch (err) {
    showError(assistantRow, err.message || "요청 처리 실패");
  } finally {
    state.busy = false;
    if (elements.sendButton) elements.sendButton.disabled = false;
    if (elements.messageInput) elements.messageInput.focus();
  }
}

async function pollMessageStatus(conversationId, messageId, row) {
  stopPolling();
  const controller = new AbortController();
  state.pollingController = controller;

  for (let attempt = 0; attempt < MAX_POLL_ATTEMPTS; attempt++) {
    if (controller.signal.aborted) return;
    try {
      const payload = await apiFetch(
        `/custom/chat/api/agents/${encodeURIComponent(state.selectedAgentId)}/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}`,
        { signal: controller.signal }
      );
      updateAssistantMessageUI(row, payload);
      const msgObj = payload.message || payload;
      const status = msgObj.status || "SUBMITTED";
      if (TERMINAL_STATUSES.has(status)) {
        await loadConversations(); // 목록 최신화
        return;
      }
    } catch (err) {
      if (err.name === "AbortError") return;
      console.warn("Polling retry...", err);
    }
    await new Promise((res) => setTimeout(res, POLL_INTERVAL_MS));
  }
}

function showError(row, message) {
  const answer = row.querySelector(".answer-content");
  answer.className = "answer-content error-card";
  answer.textContent = message;
  const statusSpan = row.querySelector(".message-status");
  if (statusSpan) statusSpan.textContent = "처리 실패";
  scrollToBottom();
}

// 대화 관리 (이름 변경, 삭제, ID 복사)
async function handleRenameConversation(conv) {
  const newTitle = prompt("새 대화 제목을 입력하세요:", conv.title || "");
  if (!newTitle || !newTitle.trim()) return;
  try {
    await apiFetch(
      `/custom/chat/api/agents/${encodeURIComponent(endpointAgentIdForMode(conv.conversation_mode))}/conversations/${encodeURIComponent(conv.conversation_id)}?mode=${conv.conversation_mode}`,
      {
        method: "PATCH",
        body: JSON.stringify({ title: newTitle.trim() }),
      }
    );
    showToast("대화 제목이 변경되었습니다.");
    await loadConversations();
  } catch (err) {
    showToast(`제목 변경 실패: ${err.message}`);
  }
}

function handleCopyId(conversationId) {
  navigator.clipboard.writeText(conversationId).then(
    () => showToast("Conversation ID가 복사되었습니다."),
    () => showToast(`복사 실패: ${conversationId}`)
  );
}

async function handleDeleteConversation(conv) {
  if (!confirm(`'${conv.title || "이 대화"}'를 삭제하시겠습니까?`)) return;
  try {
    await apiFetch(
      `/custom/chat/api/agents/${encodeURIComponent(endpointAgentIdForMode("CHAT"))}/conversations/${encodeURIComponent(conv.conversation_id)}`,
      { method: "DELETE" }
    );
    showToast("대화가 삭제되었습니다.");
    if (state.activeConversationId === conv.conversation_id) {
      startNewChat();
    }
    await loadConversations();
  } catch (err) {
    showToast(`대화 삭제 실패: ${err.message}`);
  }
}

// 이벤트 바인딩
function bindEvents() {
  elements.modeButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      const nextMode = button.dataset.mode;
      if (nextMode === state.mode) return;
      const current = state.agents.find((agent) => agent.agent_id === state.selectedAgentId);
      if (nextMode === "AGENT" && !current?.agent_mode_id) {
        showToast("선택한 Genie는 Agent 모드를 지원하지 않습니다.");
        return;
      }
      state.mode = nextMode;
      elements.modeButtons.forEach((item) => item.classList.toggle("active", item.dataset.mode === state.mode));
      startNewChat();
      await loadConversations();
    });
  });
  // 사이드바 토글
  if (elements.collapseSidebarButton) {
    elements.collapseSidebarButton.addEventListener("click", () => {
      const collapsed = document.body.classList.toggle("sidebar-collapsed");
      elements.collapseSidebarButton.textContent = collapsed ? "›" : "‹";
      elements.collapseSidebarButton.setAttribute(
        "aria-label",
        collapsed ? "사이드바 펼치기" : "사이드바 접기"
      );
    });
  }
  if (elements.openSidebarButton) {
    elements.openSidebarButton.addEventListener("click", () => {
      elements.sidebar.classList.add("mobile-open");
      elements.sidebarBackdrop.classList.add("show");
    });
  }
  if (elements.sidebarBackdrop) {
    elements.sidebarBackdrop.addEventListener("click", () => {
      elements.sidebar.classList.remove("mobile-open");
      elements.sidebarBackdrop.classList.remove("show");
    });
  }

  // 새 대화 버튼
  if (elements.newChatButton) elements.newChatButton.addEventListener("click", startNewChat);
  if (elements.headerNewChatButton) elements.headerNewChatButton.addEventListener("click", startNewChat);

  // 대화 검색 및 새로고침
  if (elements.historySearchInput) {
    elements.historySearchInput.addEventListener("input", renderHistoryList);
  }
  if (elements.refreshHistoryButton) {
    elements.refreshHistoryButton.addEventListener("click", loadConversations);
  }

  // ID 복사
  if (elements.copyConversationButton) {
    elements.copyConversationButton.addEventListener("click", () => {
      if (state.activeConversationId) handleCopyId(state.activeConversationId);
    });
  }

  // Agent 선택 변경
  if (elements.agentSelect) {
    elements.agentSelect.addEventListener("change", async (e) => {
      if (state.busy) {
        e.target.value = state.selectedAgentId;
        showToast("응답이 완료된 후 Agent를 변경해 주세요.");
        return;
      }
      state.selectedAgentId = e.target.value;
      const current = state.agents.find((a) => a.agent_id === state.selectedAgentId);
      if (current && elements.agentName) elements.agentName.textContent = current.agent_name;
      ensureSupportedMode(current);
      startNewChat();
      await loadConversations();
    });
  }

  // 입력창 및 폼 전송
  if (elements.composerForm) {
    elements.composerForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const content = elements.messageInput.value.trim();
      if (!content) return;
      elements.messageInput.value = "";
      resizeComposer();
      sendMessage(content);
    });
  }

  if (elements.messageInput) {
    elements.messageInput.addEventListener("input", resizeComposer);
    elements.messageInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        elements.composerForm.requestSubmit();
      }
    });
  }

  // 초기 추천 질문 칩들
  document.querySelectorAll(".suggestion-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      appendInputText(chip.textContent.trim());
    });
  });

  // 스크롤 감지 및 최하단 이동 버튼
  window.addEventListener("scroll", handleScroll);
  if (elements.scrollToBottomButton) {
    elements.scrollToBottomButton.addEventListener("click", () => {
      state.userScrolledUp = false;
      scrollToBottom(true);
    });
  }
}

function handleScroll() {
  if (!elements.scrollToBottomButton) return;
  if (isAtBottom()) {
    elements.scrollToBottomButton.hidden = true;
    state.userScrolledUp = false;
  } else {
    elements.scrollToBottomButton.hidden = false;
    state.userScrolledUp = true;
  }
}

function isAtBottom(threshold = 120) {
  const scrollHeight = document.documentElement.scrollHeight || document.body.scrollHeight;
  const scrollTop = window.scrollY || document.documentElement.scrollTop;
  const clientHeight = window.innerHeight;
  return scrollHeight - (scrollTop + clientHeight) <= threshold;
}

function resizeComposer() {
  if (!elements.messageInput) return;
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 180)}px`;
}

function scrollToBottom(force = false) {
  if (force || !state.userScrolledUp) {
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    });
  }
}

// 실행 진입점
document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  bootstrap();
});
