"use strict";

const $ = (selector) => document.querySelector(selector);
const state = { conversationId: null, runId: null, busy: false, controller: null, autoFollow: true };
let lastScrollY = window.scrollY;
let scrollFrame = null;

function isNearBottom() {
  return document.documentElement.scrollHeight - (window.scrollY + window.innerHeight) <= 96;
}
function updateScrollButton() {
  const button = $("#scrollToBottom");
  if (button) button.hidden = state.autoFollow || isNearBottom();
}
function scrollToLatest(force = false, behavior = "auto") {
  if (force) state.autoFollow = true;
  if (!state.autoFollow) { updateScrollButton(); return; }
  if (scrollFrame) cancelAnimationFrame(scrollFrame);
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = null;
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior });
    lastScrollY = window.scrollY;
    updateScrollButton();
  });
}

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function parseMarkdown(markdown) {
  if (!markdown) return "";
  const source = String(markdown).replace(/\r\n?/g, "\n")
    .replace(/\\\[\[(\d+)\]\((https?:\/\/[^)\s]+)\)\\\]/g, "[$1]($2)")
    .replace(/\[\[(\d+)\]\]\((https?:\/\/[^)\s]+)\)/g, "[$1]($2)");
  function safeUrl(value) {
    try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : null; }
    catch (_) { return null; }
  }
  function renderInline(value) {
    const tokens = [];
    let text = value.replace(/`([^`\n]+)`/g, (_, code) => {
      const index = tokens.push(`<code class="inline-code">${escapeHtml(code)}</code>`) - 1;
      return `\u0000${index}\u0000`;
    });
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, (match, label, href) => {
      const url = safeUrl(href); if (!url) return match;
      const visibleLabel = /^\d+$/.test(label.trim()) ? `[${label.trim()}]` : label;
      const index = tokens.push(`<a class="citation-link" href="${url}" target="_blank" rel="noopener noreferrer">${escapeHtml(visibleLabel)}</a>`) - 1;
      return `\u0000${index}\u0000`;
    });
    text = escapeHtml(text).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_\n]+)__/g, "<strong>$1</strong>").replace(/\*([^*\n]+)\*/g, "<em>$1</em>").replace(/_([^_\n]+)_/g, "<em>$1</em>");
    return text.replace(/\u0000(\d+)\u0000/g, (_, index) => tokens[Number(index)] || "");
  }
  const tableCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  const isTableDivider = (line) => { const cells = tableCells(line); return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell)); };
  const lines = source.split("\n"), blocks = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (line.trim().startsWith("```")) {
      const language = line.trim().slice(3).trim(), code = []; index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      blocks.push(`<pre class="code-block"><code${language ? ` data-language="${escapeHtml(language)}"` : ""}>${escapeHtml(code.join("\n"))}</code></pre>`); continue;
    }
    const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
    if (heading) { const level = heading[1].length; blocks.push(`<h${level} class="md-heading">${renderInline(heading[2])}</h${level}>`); index += 1; continue; }
    if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = tableCells(line); index += 2; const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) rows.push(tableCells(lines[index++]));
      blocks.push(`<div class="table-wrap"><table class="md-table"><thead><tr>${headers.map((cell) => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`); continue;
    }
    const listMatch = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]), tag = ordered ? "ol" : "ul", items = [];
      while (index < lines.length) { const match = lines[index].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/); if (!match || Boolean(match[2]) !== ordered) break; items.push(`<li>${renderInline(match[3])}</li>`); index += 1; }
      blocks.push(`<${tag}>${items.join("")}</${tag}>`); continue;
    }
    const paragraph = [line.trim()]; index += 1;
    while (index < lines.length && lines[index].trim() && !/^\s*(#{1,6})\s+/.test(lines[index]) && !/^\s*(?:[-*+]|\d+\.)\s+/.test(lines[index]) && !lines[index].trim().startsWith("```")) {
      if (lines[index].includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) break;
      paragraph.push(lines[index].trim()); index += 1;
    }
    blocks.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
  }
  return blocks.join("");
}

function card(className, text) { const element = document.createElement("article"); element.className = `message ${className}`; element.textContent = text; $("#messages").append(element); $("#empty").hidden = true; return element; }
function status(text) { $("#status").textContent = text; }
function resizeInput() { const input = $("#input"); input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 132)}px`; }
function parseFrames(buffer, onFrame) {
  const parts = buffer.replace(/\r\n/g, "\n").split("\n\n"), tail = parts.pop() || "";
  for (const block of parts) { let event = "message", id = "", data = ""; for (const line of block.split("\n")) { if (line.startsWith("event:")) event = line.slice(6).trim(); else if (line.startsWith("id:")) id = line.slice(3).trim(); else if (line.startsWith("data:")) data += line.slice(5).trim(); } if (data) onFrame(event, id, JSON.parse(data)); }
  return tail;
}
function completedMessage(data) {
  const content = data && data.item && data.item.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => part && typeof part.text === "string" ? part.text : "").filter(Boolean).join("\n");
}
function friendlyError(data) {
  const error = data && data.error;
  const code = error && (error.error_code || error.code || error.type);
  const message = ({ UPSTREAM_TIMEOUT: "Supervisor 응답 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.", UPSTREAM_ERROR: "Supervisor 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", UPSTREAM_PROTOCOL_ERROR: "Supervisor 응답 형식을 처리하지 못했습니다." })[code] || "요청을 완료하지 못했습니다. 잠시 후 다시 시도해주세요.";
  return { message, code: code || "UNKNOWN" };
}
function setBusy(busy) {
  state.busy = busy; $("#send").disabled = busy; $("#newButton").disabled = busy;
  $("#agentSelect").disabled = busy; $("#conversationSelect").disabled = busy; $("#cancel").hidden = !busy;
}
function scheduleMarkdownRender(node) {
  if (node._markdownRenderTimer) return;
  node._markdownRenderTimer = window.setTimeout(() => {
    node._markdownRenderTimer = null;
    node.innerHTML = parseMarkdown(node.dataset.raw || "");
  }, 80);
}
function flushMarkdownRender(node) {
  if (node._markdownRenderTimer) {
    window.clearTimeout(node._markdownRenderTimer);
    node._markdownRenderTimer = null;
  }
  node.innerHTML = parseMarkdown(node.dataset.raw || "");
}
function approvalDetails(data) {
  if (!data.arguments) return "외부 웹 검색을 실행하려고 합니다.";
  if (typeof data.arguments === "string") {
    try { return JSON.stringify(JSON.parse(data.arguments), null, 2); } catch (_) { return data.arguments; }
  }
  return JSON.stringify(data.arguments, null, 2);
}
function showApproval(answer, data, agentId) {
  const panel = document.createElement("section"); panel.className = "approval-panel";
  const title = document.createElement("strong"), detail = document.createElement("pre"), actions = document.createElement("div");
  title.textContent = data.name === "web_search" ? "웹 검색 승인이 필요합니다" : "도구 실행 승인이 필요합니다";
  detail.textContent = approvalDetails(data);
  const approve = document.createElement("button"), reject = document.createElement("button");
  approve.type = reject.type = "button"; approve.textContent = "웹 검색 승인"; reject.textContent = "거절";
  approve.className = "approve"; reject.className = "reject"; actions.append(approve, reject); panel.append(title, detail, actions);
  const outputNode = answer.querySelector(".markdown-body");
  if (outputNode) answer.insertBefore(panel, outputNode); else answer.prepend(panel);
  const decide = async (decision) => {
    if (state.busy) return; setBusy(true); approve.disabled = reject.disabled = true; status(decision ? "승인 처리 중" : "거절 처리 중");
    try {
      const response = await fetch(`/custom/supervisor-chat/api/agents/${encodeURIComponent(agentId)}/runs/${encodeURIComponent(state.runId)}/approvals/${encodeURIComponent(data.approval_id)}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ approve: decision }) });
      if (!response.ok) { const error = await response.json(); throw Error(error.message || `HTTP ${response.status}`); }
      panel.classList.add("decided"); panel.replaceChildren(document.createTextNode(decision ? "웹 검색을 승인했습니다." : "웹 검색을 거절했습니다."));
      await consumeResponse(response, answer, agentId);
    } catch (error) { approve.disabled = reject.disabled = false; const errorNode = document.createElement("div"); errorNode.className = "run-error"; errorNode.textContent = `승인 처리 오류: ${error.message}`; answer.append(errorNode); }
    finally { setBusy(false); status("준비"); loadConversations(state.conversationId).catch((error) => status(error.message)); }
  };
  approve.onclick = () => decide(true); reject.onclick = () => decide(false);
}
async function consumeResponse(response, answer, agentId) {
  let outputNode = answer.querySelector(".markdown-body"), progressNode = answer.querySelector(".run-progress");
  if (!outputNode) { outputNode = document.createElement("div"); outputNode.className = "markdown-body"; answer.append(outputNode); }
  if (!progressNode) { progressNode = document.createElement("div"); progressNode.className = "run-progress"; answer.append(progressNode); }
  const progress = (text) => { progressNode.hidden = !text; progressNode.textContent = text || ""; };
  let output = outputNode.dataset.raw || "", buffer = "", terminal = false, waitingApproval = false;
  const reader = response.body.getReader(), decoder = new TextDecoder();
  while (true) {
    const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true });
    buffer = parseFrames(buffer, (type, id, envelope) => {
      state.runId = envelope.run_id; state.conversationId = envelope.conversation_id; const data = envelope.data || {};
      if (type === "message.delta") { output += data.delta || ""; outputNode.dataset.raw = output; scheduleMarkdownRender(outputNode); progress("답변을 작성하고 있습니다"); }
      else if (type === "message.completed") { if (!output) output = completedMessage(data); outputNode.dataset.raw = output; flushMarkdownRender(outputNode); progress("답변을 마무리하고 있습니다"); }
      else if (type === "approval.required") { waitingApproval = true; progress(""); showApproval(answer, data, agentId); status("사용자 승인 대기"); }
      else if (type === "run.continuing") progress("추가 분석을 계속하고 있습니다");
      else if (type === "run.completed") { terminal = true; outputNode.dataset.raw = output; flushMarkdownRender(outputNode); progress(""); status("준비"); }
      else if (["run.failed", "run.timed_out", "run.cancelled"].includes(type)) { terminal = true; progress(""); const failure = type === "run.cancelled" ? { message: "요청이 중단되었습니다.", code: "CANCELLED" } : friendlyError(data); const errorNode = document.createElement("div"); errorNode.className = "run-error"; const title = document.createElement("div"), detail = document.createElement("small"); title.textContent = failure.message; detail.textContent = `실패 이벤트: ${type} · 오류 코드: ${failure.code}`; errorNode.append(title, detail); answer.append(errorNode); status("준비"); }
      else if (type.startsWith("tool.")) progress(type.endsWith("started") ? "도구를 실행하고 있습니다" : "도구 실행을 완료했습니다");
      else if (type === "run.started") progress("Supervisor가 요청을 처리하고 있습니다");
      if (id) answer.dataset.lastEventId = id;
    });
  }
  if (output) { outputNode.dataset.raw = output; flushMarkdownRender(outputNode); }
  if (!terminal && !waitingApproval) { progress("연결이 종료되었습니다"); status("준비"); }
}
async function bootstrap() {
  const response = await fetch("/custom/supervisor-chat/api/bootstrap"), body = await response.json();
  if (!response.ok) throw Error(body.message || "Agent 목록 조회 실패");
  for (const agent of body.agents) { const option = new Option(agent.name, agent.agent_id); option.dataset.longTask = String(agent.long_task); $("#agentSelect").add(option); }
  if (!body.agents.length) status("등록된 Supervisor Agent가 없습니다.");
  await loadConversations();
}
async function loadConversations(selectedId = state.conversationId) {
  const response = await fetch("/custom/supervisor-chat/api/conversations?limit=50"), body = await response.json();
  if (!response.ok) throw Error(body.message || "대화 이력 조회 실패");
  const select = $("#conversationSelect"); select.replaceChildren(new Option("대화를 선택하세요", ""));
  for (const conversation of body.conversations) {
    const title = conversation.title.length > 42 ? `${conversation.title.slice(0, 42)}…` : conversation.title;
    const option = new Option(title, conversation.conversation_id); option.dataset.agentId = conversation.agent_id; select.add(option);
  }
  if (selectedId && [...select.options].some((option) => option.value === selectedId)) select.value = selectedId;
}
async function openConversation(conversationId) {
  if (!conversationId || state.busy) return;
  const option = $("#conversationSelect").selectedOptions[0], agentId = option.dataset.agentId;
  if (agentId) $("#agentSelect").value = agentId;
  const response = await fetch(`/custom/supervisor-chat/api/agents/${encodeURIComponent(agentId)}/conversations/${encodeURIComponent(conversationId)}/items?limit=100`), body = await response.json();
  if (!response.ok) throw Error(body.message || "대화 메시지 조회 실패");
  state.conversationId = conversationId; state.runId = null; $("#messages").replaceChildren(); $("#empty").hidden = true;
  for (const item of body.items) {
    if (item.item_type === "user.message") card("user", item.content && item.content.content || "");
    else if (item.item_type === "message.completed") { const answer = card("assistant", ""), content = completedMessage(item.content || {}); answer.innerHTML = `<div class="markdown-body">${parseMarkdown(content)}</div>`; }
  }
  scrollToLatest(true);
  status("대화 이력을 불러왔습니다");
}
function reset() { if (state.busy) return; state.conversationId = null; state.runId = null; state.autoFollow = true; $("#conversationSelect").value = ""; $("#messages").replaceChildren(); $("#empty").hidden = false; updateScrollButton(); status("새 대화"); }

async function send(text) {
  setBusy(true); state.controller = new AbortController(); state.autoFollow = true;
  card("user", text); const answer = card("assistant", ""), outputNode = document.createElement("div"), progressNode = document.createElement("div");
  outputNode.className = "markdown-body"; progressNode.className = "run-progress"; progressNode.textContent = "Supervisor가 요청을 처리하고 있습니다"; answer.append(outputNode, progressNode);
  const progress = (text) => { progressNode.hidden = !text; progressNode.textContent = text || ""; };
  const key = crypto.randomUUID(), agentId = $("#agentSelect").value;
  try {
    const response = await fetch(`/custom/supervisor-chat/api/agents/${encodeURIComponent(agentId)}/responses`, { method: "POST", headers: { "content-type": "application/json", "idempotency-key": key }, body: JSON.stringify({ conversation_id: state.conversationId, input: text, long_task: $("#longTask").checked }), signal: state.controller.signal });
    if (!response.ok) { const error = await response.json(); throw Error(error.message || `HTTP ${response.status}`); }
    await consumeResponse(response, answer, agentId);
  } catch (error) {
    if (error.name !== "AbortError") { progress(""); const errorNode = document.createElement("div"); errorNode.className = "run-error"; errorNode.textContent = `요청 오류: ${error.message}`; answer.append(errorNode); status("준비"); }
  } finally {
    setBusy(false); state.controller = null;
    loadConversations(state.conversationId).catch((error) => status(error.message));
  }
}

$("#composer").addEventListener("submit", (event) => { event.preventDefault(); const value = $("#input").value.trim(); if (!value || state.busy) return; $("#input").value = ""; resizeInput(); send(value); });
$("#input").addEventListener("input", resizeInput);
$("#input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#composer").requestSubmit(); } });
$("#cancel").onclick = async () => { if (!state.runId) return; await fetch(`/custom/supervisor-chat/api/agents/${encodeURIComponent($("#agentSelect").value)}/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" }); status("중단 요청됨"); };
$("#newButton").onclick = reset; $("#agentSelect").onchange = reset; bootstrap().catch((error) => status(error.message));
$("#conversationSelect").onchange = () => openConversation($("#conversationSelect").value).catch((error) => status(error.message));
$("#scrollToBottom").onclick = () => scrollToLatest(true, "smooth");
window.addEventListener("scroll", () => {
  const currentY = window.scrollY;
  if (currentY < lastScrollY - 4 && !isNearBottom()) state.autoFollow = false;
  else if (isNearBottom()) state.autoFollow = true;
  lastScrollY = currentY;
  updateScrollButton();
}, { passive: true });
new MutationObserver(() => scrollToLatest()).observe($("#messages"), { childList: true, subtree: true, characterData: true });
