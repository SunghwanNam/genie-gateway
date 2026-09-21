"use strict";

const LIMIT_API = "/custom/admin/api/settings/supervisor-limits";

function limitField(id, label, help, disabled = false) {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.id = id; input.type = "number"; input.min = "1"; input.max = "10000"; input.disabled = disabled;
  const description = document.createElement("small"); description.textContent = help;
  wrapper.append(input, description); return wrapper;
}

function ensureLimitPanel() {
  if (document.getElementById("supervisorLimitPanel")) return;
  const main = document.querySelector("#consolePage main"); if (!main) return;
  const panel = document.createElement("section"); panel.id = "supervisorLimitPanel"; panel.className = "panel";
  panel.innerHTML = '<div class="panel-head"><div><h2>Supervisor 동시 실행 설정</h2><p>설정값은 저장 즉시 새 Run부터 적용됩니다.</p></div></div>';
  const grid = document.createElement("div"); grid.className = "limit-grid";
  grid.append(
    limitField("gatewayRunLimit", "Gateway 전체", "전체 Supervisor 활성 Run 한도"),
    limitField("userRunLimit", "사용자 전체", "한 사용자가 모든 Supervisor에서 실행 가능한 수"),
    limitField("userAgentRunLimit", "사용자·Supervisor별", "같은 Supervisor의 서로 다른 대화 실행 수"),
    limitField("conversationRunLimit", "대화별", "동일 대화는 순서 보장을 위해 1개로 고정", true),
  );
  const actions = document.createElement("div"); actions.className = "limit-actions";
  const save = document.createElement("button"); save.id = "saveSupervisorLimits"; save.className = "primary"; save.textContent = "동시 실행 설정 저장";
  actions.append(save); panel.append(grid, actions); main.append(panel);
  save.onclick = saveLimits;
}

async function loadLimits() {
  if (document.getElementById("consolePage")?.classList.contains("hidden")) return;
  ensureLimitPanel();
  const response = await fetch(LIMIT_API); if (!response.ok) return;
  const data = await response.json();
  document.getElementById("gatewayRunLimit").value = data.gateway_max_active_runs;
  document.getElementById("userRunLimit").value = data.user_max_active_runs;
  document.getElementById("userAgentRunLimit").value = data.user_agent_max_active_runs;
  document.getElementById("conversationRunLimit").value = data.conversation_max_active_runs;
}

async function saveLimits() {
  const payload = {
    gateway_max_active_runs: Number(document.getElementById("gatewayRunLimit").value),
    user_max_active_runs: Number(document.getElementById("userRunLimit").value),
    user_agent_max_active_runs: Number(document.getElementById("userAgentRunLimit").value),
  };
  const response = await fetch(LIMIT_API, { method:"PUT", headers:{"content-type":"application/json"}, body:JSON.stringify(payload) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { alert(data.detail?.[0]?.msg || data.detail || "설정을 저장하지 못했습니다."); return; }
  alert("Supervisor 동시 실행 설정을 저장했습니다."); await loadLimits();
}

const consolePage = document.getElementById("consolePage");
if (consolePage) new MutationObserver(() => loadLimits().catch(() => {})).observe(consolePage, { attributes:true, attributeFilter:["class"] });
document.getElementById("refreshButton")?.addEventListener("click", () => setTimeout(() => loadLimits().catch(() => {}), 0));
loadLimits().catch(() => {});
