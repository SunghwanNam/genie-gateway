"use strict";

window.AgentMode = (() => {
  // Agent mode의 실행 코드 상세가 다시 필요해지면 true로 변경한다.
  const SHOW_AGENT_CODE = false;

  async function responseError(response, method, path) {
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* 응답 본문이 JSON이 아닐 수 있다. */ }
    const traceId = response.headers.get("x-request-id");
    const reason = payload.detail || payload.message || "응답 본문 없음";
    const diagnostics = [
      `${method} ${path}`,
      `HTTP ${response.status}`,
      payload.error_code || payload.code,
      traceId ? `trace=${traceId}` : null,
    ].filter(Boolean).join(" · ");
    return new Error(`${reason} (${diagnostics})`);
  }

  function eventBlocks(buffer) {
    const normalized = buffer.replace(/\r\n/g, "\n");
    const splitAt = normalized.lastIndexOf("\n\n");
    if (splitAt < 0) return { events: [], rest: buffer };
    const complete = normalized.slice(0, splitAt);
    return { events: complete.split("\n\n").filter(Boolean), rest: normalized.slice(splitAt + 2) };
  }

  function parseEvent(block) {
    let name = "message";
    const data = [];
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    });
    if (!data.length || data.join("") === "[DONE]") return null;
    try { return { name, payload: JSON.parse(data.join("\n")) }; } catch (_) { return null; }
  }

  function itemFromEvent(event) {
    return event?.payload?.item || event?.payload?.output_item || null;
  }

  function textParts(value) {
    if (typeof value === "string") return [value];
    if (Array.isArray(value)) return value.flatMap(textParts);
    if (!value || typeof value !== "object") return [];
    return ["text", "output_text", "content", "output"].flatMap((key) => key in value ? textParts(value[key]) : []);
  }

  function sourceParts(value) {
    if (Array.isArray(value)) return value.flatMap(sourceParts);
    if (!value || typeof value !== "object") return [];
    const direct = [];
    if (Array.isArray(value.annotations)) direct.push(...value.annotations);
    if (Array.isArray(value.citations)) direct.push(...value.citations);
    Object.entries(value).forEach(([key, nested]) => {
      if (!["annotations", "citations"].includes(key)) direct.push(...sourceParts(nested));
    });
    return direct;
  }

  function sourceValue(source, keys) {
    if (!source || typeof source !== "object") return "";
    for (const key of keys) {
      if (typeof source[key] === "string" && source[key].trim()) return source[key].trim();
    }
    for (const nested of Object.values(source)) {
      if (nested && typeof nested === "object") {
        const found = sourceValue(nested, keys);
        if (found) return found;
      }
    }
    return "";
  }

  function functionArguments(item) {
    const raw = item?.arguments || item?.args;
    if (!raw) return {};
    if (typeof raw === "object") return raw;
    try { return JSON.parse(raw); } catch (_) { return {}; }
  }

  function queryOutput(item, call) {
    const raw = typeof item?.output === "string" ? item.output.trim() : textParts(item?.output).join("\n").trim();
    const lines = raw.split("\n");
    const firstContentIndex = lines.findIndex((line) => line.trim());
    const firstLine = firstContentIndex >= 0 ? lines[firstContentIndex].trim() : "";
    const outputHasTitle = firstLine && !firstLine.startsWith("|") && !/^no results?\.?$/i.test(firstLine);
    const body = outputHasTitle ? lines.slice(firstContentIndex + 1).join("\n").trim() : raw;
    const args = functionArguments(call);
    const empty = !body || /^(no results?|조회 결과 없음|결과 없음)[.!]?$/i.test(body);
    return {
      callId: item.call_id || call?.call_id || item.id,
      title: args.title || (outputHasTitle ? firstLine : "") || "쿼리 실행 결과",
      sql: args.sql || "",
      body,
      empty,
    };
  }

  function appendQueryCards(answer, queries, context) {
    if (!queries.length) return;
    const section = document.createElement("section");
    section.className = "agent-query-section";
    const heading = document.createElement("h3");
    heading.className = "agent-section-title";
    heading.textContent = "조회 결과";
    section.appendChild(heading);

    let expanded = false;
    queries.forEach((query) => {
      const card = document.createElement("details");
      card.className = `agent-query-card${query.empty ? " is-empty" : ""}`;
      card.dataset.callId = query.callId || "";
      if (!query.empty && !expanded) {
        card.open = true;
        expanded = true;
      }

      const summary = document.createElement("summary");
      const title = document.createElement("span");
      title.className = "agent-query-title";
      title.textContent = query.title;
      const state = document.createElement("span");
      state.className = "agent-query-state";
      state.textContent = query.empty ? "조회 결과 없음" : "결과 보기";
      summary.append(title, state);
      card.appendChild(summary);

      if (!query.empty) {
        const body = document.createElement("div");
        body.className = "agent-query-body";
        body.innerHTML = context.parseMarkdown(query.body);
        card.appendChild(body);
      }
      section.appendChild(card);
    });
    answer.appendChild(section);
  }

  function appendFinalReport(answer, finalTexts, hasQueries, context, streamCompleted) {
    const markdown = [...new Set(finalTexts.map((text) => text.trim()).filter(Boolean))].join("\n\n");
    if (!markdown && !streamCompleted) {
      const pending = document.createElement("p");
      pending.className = "agent-report-pending typing-dots";
      pending.textContent = "인사이트를 정리하고 있습니다";
      answer.appendChild(pending);
      return;
    }
    if (!markdown) return;
    const report = document.createElement("section");
    report.className = "agent-final-report";
    if (hasQueries) {
      const heading = document.createElement("h3");
      heading.className = "agent-section-title";
      heading.textContent = "인사이트 요약";
      report.appendChild(heading);
    }
    const body = document.createElement("div");
    body.className = "agent-report-body";
    body.innerHTML = context.parseMarkdown(markdown);
    report.appendChild(body);
    answer.appendChild(report);
  }

  function render(row, itemMap, context, streamCompleted = false) {
    const answer = row.querySelector(".answer-content");
    const attachments = row.querySelector(".attachment-list");
    const status = row.querySelector(".message-status");
    const entries = [...itemMap.values()];
    const completedItems = entries.filter((entry) => entry.done).map((entry) => entry.item);
    const items = completedItems;
    const finalTexts = [];
    const reasoning = [];
    const codes = [];
    const sources = [];
    const calls = new Map();
    const queryItems = [];

    items.forEach((item) => {
      const type = item.type || "";
      const values = textParts(item).filter((value) => value.trim());
      if (type === "reasoning") reasoning.push(...values);
      else if (type === "function_call") {
        const raw = item.arguments || item.args || values.join("\n");
        if (raw) codes.push(typeof raw === "string" ? raw : JSON.stringify(raw, null, 2));
        if (item.call_id) calls.set(item.call_id, item);
      } else if (type === "function_call_output") queryItems.push(item);
      else if (type === "message") {
        if (item.role !== "user") finalTexts.push(...values);
      } else finalTexts.push(...values);
      sources.push(...sourceParts(item));
    });

    const queries = queryItems.map((item) => queryOutput(item, calls.get(item.call_id)));

    answer.className = "answer-content";
    answer.innerHTML = "";
    appendQueryCards(answer, queries, context);
    appendFinalReport(answer, finalTexts, queries.length > 0, context, streamCompleted);
    if (!answer.childElementCount) answer.textContent = streamCompleted ? "완료된 응답이 없습니다." : "응답을 구성하고 있습니다…";
    attachments.innerHTML = "";
    if (reasoning.length) {
      const details = document.createElement("details");
      details.className = "attachment-card agent-reasoning";
      details.innerHTML = `<summary>처리 과정</summary><div>${context.parseMarkdown([...new Set(reasoning)].join("\n\n"))}</div>`;
      attachments.appendChild(details);
    }
    // 운영 화면에서는 코드 표시를 숨긴다. SHOW_AGENT_CODE=true로 복원할 수 있다.
    if (SHOW_AGENT_CODE) {
      [...new Set(codes.map((code) => code.trim()).filter(Boolean))].forEach((code) => {
        const details = document.createElement("details");
        details.className = "attachment-card";
        const summary = document.createElement("summary");
        summary.textContent = "코드 표시";
        const pre = document.createElement("pre");
        pre.className = "sql-code";
        pre.textContent = code;
        details.append(summary, pre);
        attachments.appendChild(details);
      });
    }
    if (sources.length) {
      const uniqueSources = [...new Map(sources.map((source, index) => {
        const url = sourceValue(source, ["url", "uri", "href", "source_url"]);
        const key = url || JSON.stringify(source);
        return [key, { source, url, index }];
      })).values()];
      const details = document.createElement("details");
      details.className = "attachment-card agent-source-list";
      const summary = document.createElement("summary");
      summary.textContent = `소스 (${uniqueSources.length})`;
      const list = document.createElement("ol");
      uniqueSources.forEach(({ source, url }, index) => {
        const item = document.createElement("li");
        const label = sourceValue(source, ["title", "name", "label"]) || "링크 참고";
        if (/^https?:\/\//i.test(url)) {
          const link = document.createElement("a");
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = `[${index + 1}] ${label}`;
          item.appendChild(link);
        } else {
          item.textContent = `[${index + 1}] ${label}`;
        }
        list.appendChild(item);
      });
      details.append(summary, list);
      attachments.appendChild(details);
    }
    status.textContent = streamCompleted ? "완료" : "처리 중";
    context.scrollToBottom();
  }

  function addActions(row, agentId, conversationId, showToast) {
    if (!conversationId || row.querySelector(".agent-actions")) return;
    const actions = document.createElement("div");
    actions.className = "agent-actions";
    const pdf = document.createElement("button");
    pdf.type = "button";
    pdf.textContent = "↓ PDF 다운로드";
    pdf.addEventListener("click", () => {
      window.location.href = `/custom/chat/api/agents/${encodeURIComponent(agentId)}/agent/conversations/${encodeURIComponent(conversationId)}/reports/pdf`;
    });
    const share = document.createElement("button");
    share.type = "button";
    share.textContent = "공유";
    share.addEventListener("click", async () => {
      const url = new URL(window.location.href);
      url.searchParams.set("mode", "agent");
      url.searchParams.set("agent", agentId);
      url.searchParams.set("conversation", conversationId);
      try {
        if (navigator.share) await navigator.share({ title: "Genie Agent 분석", url: url.toString() });
        else await navigator.clipboard.writeText(url.toString());
        showToast(navigator.share ? "공유 창을 열었습니다." : "공유 링크를 복사했습니다.");
      } catch (error) {
        if (error.name !== "AbortError") showToast("공유에 실패했습니다.");
      }
    });
    actions.append(pdf, share);
    row.querySelector(".assistant-content").appendChild(actions);
  }

  async function send(context) {
    const path = `/custom/chat/api/agents/${encodeURIComponent(context.agentId)}/agent/responses`;
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ content: context.content, conversation_id: context.conversationId, enable_viz: true }),
    });
    if (!response.ok) throw await responseError(response, "POST", path);
    if (!response.body) throw new Error(`SSE 응답 본문이 없습니다. (POST ${path} · HTTP ${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const items = new Map();
    let buffer = "";
    let conversationId = context.conversationId;
    let announcedConversationId = context.conversationId;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const parsed = eventBlocks(buffer);
      buffer = parsed.rest;
      parsed.events.map(parseEvent).filter(Boolean).forEach((event) => {
        conversationId = event.payload?.response?.conversation_id || event.payload?.conversation_id || conversationId;
        if (conversationId && conversationId !== announcedConversationId) {
          announcedConversationId = conversationId;
          if (typeof context.onConversationCreated === "function") {
            void Promise.resolve(context.onConversationCreated(conversationId)).catch((error) => {
              console.warn("Agent 대화 이력 즉시 갱신 실패:", error);
            });
          }
        }
        const item = itemFromEvent(event);
        const eventType = event.payload?.type || event.name;
        const isItemDone = eventType === "response.output_item.done" || event.name === "response.output_item.done";
        if (item) {
          const outputIndex = event.payload?.output_index;
          const key = item.id || item.item_id || (Number.isInteger(outputIndex) ? `output-${outputIndex}` : `${item.type}-pending`);
          const previous = items.get(key);
          items.set(key, { item: { ...(previous?.item || {}), ...item }, done: Boolean(previous?.done || isItemDone) });
        }
        const isCompleted = eventType === "response.completed" || event.name === "response.completed";
        const isFailed = eventType === "response.failed" || event.name === "response.failed";
        if (isCompleted && Array.isArray(event.payload?.response?.output)) {
          event.payload.response.output.forEach((outputItem, outputIndex) => {
            const key = outputItem.id || outputItem.item_id || `output-${outputIndex}`;
            const previous = items.get(key);
            items.set(key, { item: { ...(previous?.item || {}), ...outputItem }, done: true });
          });
        }
        render(context.row, items, context, isCompleted);
        if (isCompleted || isFailed) {
          context.row.querySelector(".message-status").textContent = isFailed ? "처리 실패" : "완료";
        }
      });
      if (done) break;
    }
    render(context.row, items, context, true);
    addActions(context.row, context.agentId, conversationId, context.showToast);
    return { conversationId };
  }

  async function loadConversation(context) {
    const path = `/custom/chat/api/agents/${encodeURIComponent(context.agentId)}/agent/conversations/${encodeURIComponent(context.conversationId)}/items?limit=100&order=asc`;
    const response = await fetch(path);
    if (!response.ok) throw await responseError(response, "GET", path);
    const payload = await response.json();
    const rawItems = payload.items || payload.data || [];
    const outputs = new Map();
    let row = null;
    rawItems.forEach((item, index) => {
      if (item.role === "user" || item.type === "input_message") {
        outputs.clear();
        const text = textParts(item).join("\n");
        if (text) context.addUserMessageUI(text);
        row = null;
      } else {
        if (!row) row = context.addAssistantMessageUI();
        outputs.set(item.id || item.item_id || `history-${index}`, { item, done: true });
        render(row, outputs, context, true);
      }
    });
    if (row) {
      row.querySelector(".message-status").textContent = "완료";
      addActions(row, context.agentId, context.conversationId, context.showToast);
    }
    context.scrollToBottom();
  }

  return { send, loadConversation };
})();
